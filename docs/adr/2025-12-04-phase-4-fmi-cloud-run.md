# ADR-2025-12-04: Phase 4 - FMI Cloud Run Jobs Implementation

**Status:** Proposed
**Owner:** Platform / Validation Runtime
**Created:** 2025-12-04
**Related ADRs:**
- [2025-12-04: Validator Job Interface Contract](2025-12-04-validator-job-interface.md)
- [2025-11-20: FMI Storage and Security Review](2025-11-20-fmi-storage-and-security-review.md)
- [2025-11-17: FMI Validator](completed/2025-11-17-FMI-Validator.md)
- [2025-11-17: FMI Validator Update](completed/2025-11-17-FMI-Validator-update.md)

---

## 1. Context

We've completed the initial Cloud Run migration (Phases 1-3) with EnergyPlus validator support. The existing FMI/FMU implementation still contains Modal-specific code that needs to be refactored to use Cloud Run Jobs.

### What FMI Validators Do

FMI (Functional Mock-up Interface) validators allow users to run simulation models (FMUs) as part of validation workflows. An FMU is a standardized simulation component that takes inputs (e.g., temperature setpoint, occupancy schedule) and produces outputs (e.g., energy consumption, comfort metrics).

The key value proposition: workflow authors can use FMUs to simulate building systems, HVAC equipment, or control logic, then apply CEL assertions to the simulation outputs.

### The Three User Roles

Understanding FMI validation requires understanding three distinct user journeys:

| Role | What they do | Example |
|------|--------------|---------|
| **Validator Author** | Creates and configures the FMI validator by uploading an FMU, selecting which inputs/outputs to expose | An engineer uploads a heat pump FMU, exposes "outdoor_temp" as input and "COP" as output |
| **Workflow Author** | Adds the FMI validator to a workflow step, binds inputs to submission fields, writes CEL assertions against outputs | Maps submission's "design_temperature" field to FMU's "outdoor_temp", adds assertion `COP > 3.0` |
| **Workflow User** | Submits data to trigger the workflow, receives validation results | Submits a building design with design_temperature=-10°C, gets pass/fail based on COP assertion |

---

## 2. User Journeys

### Journey 1: Validator Author Creates FMI Validator

**Goal:** Upload an FMU and configure which variables are available for workflow authors.

**Steps:**

1. **Create new FMI Validator** (Validator Library → New FMI Validator)
   - Enter name, description, project
   - Upload FMU file (e.g., `HeatPump.fmu`)

2. **System introspects FMU**
   - Parses `modelDescription.xml` from the FMU ZIP
   - Extracts all `ScalarVariable` entries with causality (input/output/parameter)
   - Creates `FMIVariable` records for each variable

3. **Select exposed variables**
   - Author sees all detected inputs and outputs
   - Selects which to expose in the validator catalog
   - For each exposed variable, a `ValidatorCatalogEntry` is created with:
     - `slug`: sanitized variable name (e.g., `outdoor_temp`)
     - `run_stage`: INPUT or OUTPUT
     - `data_type`: inferred from FMU (Real→NUMBER, Integer→NUMBER, Boolean→BOOLEAN)
     - `is_required`: author sets for inputs
     - `is_hidden`: author can hide variables with default values

4. **Run probe test**
   - Author provides test inputs (JSON)
   - Author provides expected outputs (JSON)
   - System runs short probe simulation via Cloud Run Job
   - If outputs match expected (within tolerance), FMU is marked `is_approved=True`
   - If probe fails, author sees errors and can retry

5. **Validator is ready**
   - Validator appears in library with `is_approved=True`
   - Workflow authors can now add it to workflow steps

**Data created:**
- `Validator` (type=FMI, linked to FMUModel)
- `FMUModel` (file stored in GCS, checksum, metadata)
- `FMIVariable` (one per variable in modelDescription.xml)
- `ValidatorCatalogEntry` (one per exposed input/output)
- `FMUProbeResult` (probe status and details)

### Journey 2: Workflow Author Adds FMI Step

**Goal:** Use the FMI validator in a workflow, binding inputs and writing assertions.

**Steps:**

1. **Add FMI step to workflow**
   - Workflow editor → Add Step → Select FMI validator
   - Step is created with reference to the validator

2. **Review inputs/outputs** (no custom signals)
   - Inputs/outputs (and optional bindings) are defined by the validator author during FMU introspection. Workflow authors cannot add or rename signals.
   - For each required input catalog entry, the binding path is taken from the catalog entry. If no binding is provided, we default to matching the top-level submission key with the catalog slug (UI should remind authors of this default).

3. **Create ruleset with assertions**
   - Create or select a Ruleset for this step
   - Write CEL assertions referencing output catalog entries:
     ```cel
     COP > 3.0
     energy_consumption < 1000
     ```
   - Assertions stored in `Ruleset.content`

4. **Workflow is ready**
   - Workflow can be activated
   - Users can submit data to trigger validation runs

**Data created/modified:**
- `ValidatorCatalogEntry` rows define the exposed signals (inputs/outputs) for the validator (one per exposed FMU variable). These are created during validator authoring. Each input entry carries an optional binding path (submission field path). If omitted, the binding defaults to the catalog slug as a top-level submission key.
- `Ruleset` (CEL assertions referencing catalog entry slugs)

### Journey 3: Workflow User Runs Validation

**Goal:** Submit data and receive validation results.

**Steps:**

1. **User submits data**
   - Uploads file or submits JSON via API
   - Creates `Submission` record

2. **Workflow execution starts**
   - Creates `ValidationRun` (status=PENDING)
   - Creates `StepRun` for the FMI step (status=PENDING)

3. **FMI engine prepares execution**
   - Resolves input values using catalog entry bindings (or slug-name defaults) against the submission payload. No per-step remapping.
   - Builds `FMIInputEnvelope` with:
     - FMU file URI (from FMUModel.gcs_uri or local path in dev)
     - Resolved input values keyed by catalog slugs
     - Simulation config
     - Callback URL and JWT token
   - Uploads envelope to storage (GCS in cloud, local path in dev) as `input.json`
   - Triggers Cloud Run Job
   - Updates step status to RUNNING

4. **Cloud Run Job executes** (async)
   - Downloads FMU from GCS
   - Runs FMPy simulation with inputs
   - Captures output values
   - Writes `output.json` to GCS
   - POSTs callback to Django

5. **Django processes callback**
   - Verifies JWT token
   - Downloads `output.json` from GCS
   - Extracts output values → creates signal values in context
  - Runs CEL assertions against outputs
  - Updates `StepRun` with pass/fail and issues (persists messages as ValidationFindings and recomputes summaries)
  - Updates `ValidationRun` status

6. **User sees results**
   - Validation run shows pass/fail
   - Individual assertions show which passed/failed
   - FMU output values visible in run details

---

## 3. Technical Design

### 3.1 FMU Storage Migration (Modal → GCS)

**New flow (GCS/local):**
```
Upload FMU → Validate ZIP → Upload to GCS → Store gcs_uri
```

**Model changes to FMUModel:**
```python
class FMUModel(models.Model):
    # Existing fields remain
    file = models.FileField(...)  # Local storage during upload/dev
    checksum = models.CharField(...)

    # Add GCS URI for cloud storage
    gcs_uri = models.URLField(
        blank=True,
        help_text="GCS URI to the FMU (e.g., gs://bucket/fmus/{checksum}.fmu)"
    )
```

**FMU storage location:** `gs://{GCS_VALIDATION_BUCKET}/fmus/{checksum}.fmu`

Using checksum as filename enables deduplication - same FMU uploaded twice only stored once.

Execution-time policy: Cloud Run Jobs should read the FMU directly from this canonical GCS URI (no copy into each execution bundle) to avoid duplication and keep checksum-based caching effective. If we later need per-run isolation we can add an optional “copy into bundle” flag, but the default remains direct read.

### 3.2 FMI Envelope Schemas

Create `sv_shared/fmi/envelopes.py` following the EnergyPlus pattern:

**FMIInputs (simulation configuration):**
```python
class FMIInputs(BaseModel):
    """FMI simulation configuration parameters."""

    # Simulation time settings
    start_time: float = Field(default=0.0, description="Simulation start time (seconds)")
    stop_time: float = Field(default=1.0, description="Simulation stop time (seconds)")
    step_size: float = Field(default=0.01, description="Communication step size (seconds)")

    # Input variable values (resolved from bindings)
    input_values: dict[str, Any] = Field(
        default_factory=dict,
        description="Input variable values (catalog_slug -> value)"
    )

    # Output configuration
    output_variables: list[str] = Field(
        default_factory=list,
        description="Output catalog slugs to capture"
    )

    # Solver settings (optional)
    tolerance: float | None = Field(default=None)
```

**FMIOutputs (execution results):**
```python
class FMIOutputs(BaseModel):
    """FMI simulation outputs and execution information."""

    # Output variable values (final values at stop_time)
    output_values: dict[str, Any] = Field(
        default_factory=dict,
        description="Output variable values (catalog_slug -> value)"
    )

    # FMU metadata (from execution)
    fmu_guid: str | None = None
    fmi_version: str | None = None
    model_name: str | None = None

    # Execution timing
    execution_seconds: float
    simulation_time_reached: float

    # Logs (optional)
    fmu_log: str | None = None
```

**Input file roles:**
```python
# Primary FMU file
InputFileItem(
    name="model.fmu",
    mime_type=SupportedMimeType.FMU,
    role="fmu",
    uri="gs://bucket/fmus/{checksum}.fmu"
)

# Optional: timeseries input data (future)
InputFileItem(
    name="inputs.csv",
    mime_type="text/csv",
    role="timeseries-input",
    uri="gs://bucket/runs/{run_id}/inputs.csv"
)
```

### 3.3 Envelope Builder

Add to `simplevalidations/validations/services/cloud_run/envelope_builder.py`:

```python
def build_fmi_input_envelope(
    *,
    run_id: str,
    validator: Validator,
    org_id: str,
    org_name: str,
    workflow_id: str,
    step_id: str,
    step_name: str | None,
    fmu_uri: str,  # GCS URI to FMU file
    input_values: dict[str, Any],  # Resolved input bindings
    output_variables: list[str],  # Catalog slugs to capture
    callback_url: str,
    callback_token: str,
    execution_bundle_uri: str,
    start_time: float = 0.0,
    stop_time: float = 1.0,
    step_size: float = 0.01,
) -> FMIInputEnvelope:
    """Build an FMIInputEnvelope from Django validation run data."""
    ...
```

### 3.3.1 Envelope Types (align with Validator Job Interface ADR)

- Input envelope should include:
  - `input_files`: primary FMU file as `gs://.../fmus/{checksum}.fmu` with role `fmu`
  - `inputs`: simulation config + resolved `input_values` keyed by catalog slugs
  - `context`: callback URL/token and execution bundle URI (for outputs)
- Output envelope should include:
  - `messages`: validation findings (errors/warnings/info) for storage as `ValidationFinding`
  - `metrics`: any high-level metrics from the FMU run
  - `outputs`: `FMIOutputs` with `output_values`, timing, and FMU metadata
  - `raw_outputs`: optional manifest if the job writes additional artifacts

### 3.4 Launcher Function

Add to `simplevalidations/validations/services/cloud_run/launcher.py`:

```python
def launch_fmi_validation(
    *,
    run: ValidationRun,
    validator: Validator,
    submission: Submission,
    ruleset: Ruleset | None,
    step: WorkflowStep,
) -> ValidationResult:
    """
    Launch an FMI validation via Cloud Run Jobs.

    Flow:
    1. Get FMU model and GCS URI
    2. Resolve input bindings from catalog entries (default to slug-name lookup)
    3. Build FMIInputEnvelope
    4. Upload envelope to GCS
    5. Trigger Cloud Run Job
    6. Return pending ValidationResult
    """
    ...
```

### 3.5 Catalog Bindings (Inputs)

- Each input `ValidatorCatalogEntry` carries an optional binding path indicating where to pull the value from the submission payload (e.g., `design.temperature`).
- If no binding path is provided, we default to a top-level key that matches the catalog slug.
- Workflow authors cannot change bindings; they are fixed at validator-author time. Missing required inputs (catalog `is_required=True`) cause a pre-flight failure before triggering the Cloud Run Job.

### 3.6 FMI Engine Update

Update `simplevalidations/validations/engines/fmi.py`:

```python
@register_engine(ValidationType.FMI)
class FMIValidationEngine(BaseValidatorEngine):
    """Run FMI validators through Cloud Run Jobs."""

    def validate_with_run(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset | None,
        run: ValidationRun,
        step: WorkflowStep,
    ) -> ValidationResult:
        """Validate submission using FMI Cloud Run Job."""

        provider = self.resolve_provider(validator)
        if provider:
            provider.ensure_catalog_entries()

        # Check if Cloud Run Jobs is configured
        if not settings.GCS_VALIDATION_BUCKET or not settings.GCS_FMI_JOB_NAME:
            return ValidationResult(
                passed=False,
                issues=[ValidationIssue(
                    path="",
                    message="FMI Cloud Run Jobs not configured.",
                    severity=Severity.ERROR,
                )],
                stats={"status": "not_configured"},
            )

        from simplevalidations.validations.services.cloud_run.launcher import (
            launch_fmi_validation,
        )

        return launch_fmi_validation(
            run=run,
            validator=validator,
            submission=submission,
            ruleset=ruleset,
            step=step,
        )
```

---

## 4. Phase Scope and Interim Approvals

- **Phase 4 (this ADR):** Django-side plumbing (models, envelopes, bindings, callback handler, GCS storage) with FMU reads from `gcs_uri`. No Cloud Run Job container yet.
- **Phase 4b:** Implement and deploy the FMI Cloud Run Job container and probe container, then switch execution to Cloud Run.
- **Probe approvals while Modal is removed:** Until the probe container exists (Phase 4b), `create_fmi_validator()` should temporarily auto-approve FMUs after checksum/introspection to keep author flows unblocked. When Phase 4b ships, restore probe-based approval and stop auto-approving.

---

## 5. Implementation Plan

### Step 1: Create FMI envelope schemas (sv_shared)

Create `sv_shared/fmi/envelopes.py`:
- `FMIInputs` - simulation configuration model
- `FMIOutputs` - execution results model
- `FMIInputEnvelope(ValidationInputEnvelope)` - typed input envelope
- `FMIOutputEnvelope(ValidationOutputEnvelope)` - typed output envelope

Update `sv_shared/fmi/__init__.py` exports.

### Step 2: Add GCS field to FMUModel (Django)

- Add `gcs_uri` field to `FMUModel`
- Create migration
- Update `create_fmi_validator()` to upload to GCS instead of Modal
- Keep `file` field for local dev fallback

### Step 3: Add FMI envelope builder (Django)

Add `build_fmi_input_envelope()` to envelope_builder.py following EnergyPlus pattern.

### Step 4: Implement FMI launcher (Django)

Add `launch_fmi_validation()` to launcher.py:
- Resolve input bindings
- Upload FMU to GCS execution bundle (copy from FMUModel.gcs_uri)
- Build envelope
- Trigger Cloud Run Job

### Step 5: Refactor fmi.py service (Django)

- Remove `_cache_fmu_in_modal_volume()` and `_upload_to_modal_volume()`
- Remove Modal imports
- Add `_upload_fmu_to_gcs()` function
- Update `create_fmi_validator()` to use GCS

### Step 6: Update FMI engine (Django)

- Add `validate_with_run()` method
- Remove stub implementation
- Call `launch_fmi_validation()`

### Step 7: Add settings (Django)

- `GCS_FMI_JOB_NAME = "validibot-validator-fmi"`

---

## 6. What's NOT in Phase 4

Deferred to Phase 4b or later:

1. **FMI Cloud Run Job container** - The actual container with FMPy
2. **Real probe implementation** - Currently auto-approves; real probing needs container
3. **Timeseries input support** - Envelope supports it, implementation deferred
4. **Input binding UI** - Workflow editor UI for configuring bindings
5. **Co-simulation mode** - Not in scope

---

## 7. Error/Message Handling for Advanced Validators

Advanced validators (FMI, EnergyPlus, etc.) must surface findings the same way as "simple" validators:

- Validator jobs populate `messages` in the output envelope (severity INFO/WARNING/ERROR), plus optional `metrics` and `artifacts`.
- The callback handler persists these messages as `ValidationFinding` rows, recomputes run/step summaries, and returns them to the launcher just like basic validations.
- Consumers (API/UI) see a consistent error/info format regardless of validator type.

---

## 8. Testing Strategy

### Unit tests
- FMI envelope serialization/deserialization
- `build_fmi_input_envelope()` with mock data
- `launch_fmi_validation()` with mocked GCS/Cloud Tasks

### Integration tests
- FMU upload to GCS flow
- End-to-end test with mocked Cloud Run Job callback

---

## 9. Success Criteria

- [ ] FMI envelope schemas created and exported from sv_shared
- [ ] `FMUModel.gcs_uri` field added with migration
- [ ] Modal code removed from `services/fmi.py`
- [ ] `build_fmi_input_envelope()` works with test data
- [ ] `launch_fmi_validation()` uploads envelope and triggers job
- [ ] `FMIValidationEngine.validate_with_run()` returns pending result
- [ ] All existing tests pass
- [ ] Documentation updated

---

## 10. Open Questions (Resolved)

These questions have been resolved during ADR review:

1. **Input binding resolution** - Bindings live on `ValidatorCatalogEntry` (optional path); default is top-level key matching the catalog slug. Workflow authors cannot remap bindings. Required inputs must resolve or the launch fails fast.

2. **FMU caching** - Jobs read directly from the canonical `gs://.../fmus/{checksum}.fmu` path in cloud; local runs use filesystem storage.

3. **Probe implementation** - Auto-approve post-introspection until Phase 4b delivers the probe container.

4. **Security/IAM and job invocation** - Cloud Run Jobs execute under a dedicated service account with bucket read access (prefer CMEK-backed buckets). Callbacks must carry JWT claims for `run_id`, `step_run_id`, `validator_id`, and `org_id`; Django verifies all claims before processing. Jobs use `INPUT_URI` and load the FMU from the `input_files` entry (role=`fmu`), respecting `context.timeout_seconds` for max runtime. Status values map to `StepStatus`/`ValidationRunStatus` as in the Validator Job Interface ADR.

5. **Binding validation and CEL assertions** - Required inputs (catalog `is_required`) must resolve before enqueue. On callback, output values surface to CEL under the catalog slugs for the current step; downstream steps use a namespaced `steps.<step_id>.<slug>` to avoid collisions. Findings/metrics persist the same way as basic validators.

---

## 11. Next Steps After Phase 4

1. **Phase 4b: FMI Cloud Run Job Container**
   - Build container with FMPy
   - Implement envelope parsing
   - Test with real FMU simulations

2. **Phase 4c: FMU Probe via Cloud Run**
   - Implement real probe using container
   - Update probe status flow

3. **Phase 4d: Input Binding UI**
   - Workflow editor UI for configuring input bindings
   - Visual mapping of submission fields to FMU inputs
