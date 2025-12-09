# ADR-2015-11-17-FMI-Validator: Introduce FMI-Based Validator and Modal.com Execution

## Status

Proposed (2025-11-17)

## Context

Validibot is evolving from “schema-only” validations (JSON Schema, XML Schema, CEL-based checks) toward richer, simulation-backed validations. Many engineering-oriented users model system behavior using the Functional Mock-up Interface (FMI) standard and export models as FMUs (Functional Mock-up Units).

There is a strong use case for allowing workflow authors to:

- Upload an FMU (FMI 2.0/3.0),
- Automatically discover its inputs and outputs,
- Bind those inputs to submission fields / derived signals,
- Run the FMU as part of a validation workflow step, and
- Use the resulting outputs in CEL-based assertions.

However, FMUs are effectively untrusted native code wrapped in a zip. Running them directly in the Validibot web/app infrastructure would significantly increase security risk. FMUs may also be designed for real-time or long-running co-simulation, which does not fit the “short, one-shot validation” model.

We need a design that:

- Exposes FMI-based validation in a simple way to workflow authors,
- Automatically surfaces FMU IO as Catalog entries / signals for CEL,
- Uses a safe execution environment (Modal.com) for untrusted FMUs,
- Enforces constraints on which FMUs are acceptable (e.g., short runs, limited resource usage),
- Keeps the core Django app stateless with respect to simulation.

## Decision

We will:

1. **Introduce an “FMI Validator” step type** that runs an FMU as part of a workflow via Modal.com.
2. **Store FMUs as versioned assets (e.g., in S3) and never execute them on the web/app servers.**
3. **Introspect FMUs automatically** (via `modelDescription.xml`) to detect variables and wire them into the Validator Catalog.
4. **Execute FMUs only in a sandboxed Modal.com container** with strict limits (CPU, memory, runtime) and no access to core infra or secrets.
5. **Apply safety checks** (size, metadata, short “probe run”) before marking FMUs as approved for use in workflows. This will be part of the validator creation process.
6. **Constrain the usage semantics** to short, one-shot simulations that behave like pure-ish functions `(inputs, config) → outputs`.

### 1. FMI Validator Step Type and Authoring Flow

We introduce a new Validator step type, conceptually:

- **Name:** `FMIValidatorStep`
- **Purpose:** run a configured FMU with bound inputs, obtain outputs, and make them available as signals for CEL checks.

**Data model alignment**

- Add `ValidationType.FMI`; reuse existing `Validator`/`WorkflowStep`/`Ruleset` tables.
- Use `FMUModel` (not “FMIModel”), scoped to `Project`/org and referenced by the FMI validator. Typically 1:1, but reuse is allowed if we want multiple validators with different assertions/bindings over the same FMU.
- IO is exposed via `ValidatorCatalogEntry` rows tied to the FMI validator. We use existing fields (`entry_type` SIGNAL/DERIVATION, `run_stage` INPUT/OUTPUT, `is_required`). Add a `hidden` flag (default False) to allow the validator author to hide specific signals; for hidden signals, allow an optional `default_value` constrained by the data type. Store FMU variable names as-is (sanitized, no renaming).
- Catalog entries define the FMU variable-to-signal mapping. Workflow step bindings (where to source input values) are per-step: the step config maps submission fields/other signals → input catalog entry slugs. The Ruleset continues to hold assertions only and references catalog entry slugs; signals are not stored in Ruleset content.
- Role requirements: only `OWNER/ADMIN/AUTHOR` in the current org can create/manage FMI validators and FMUs; `EXECUTOR` can run workflows; `VALIDATION_RESULTS_VIEWER` can read results org-wide.

**UI authoring flow (UI-only, full-page wizard)**

1. Create draft validator + upload FMU

- Fields: name/slug/description/project/org; FMU upload.
- Enforce limits (`MAX_FMU_SIZE`, supported FMI versions/kinds).
- Persist draft `Validator` (type=FMI, is_approved=False) + `FMUModel` and enqueue async introspection/probe job. Show read-only state with a Cancel button (deletes validator, FMUModel, catalog drafts, probe task).

2. Introspection & IO selection

- Parse `modelDescription.xml`, collect variables, persist `FMIVariable`.
- Enforce `MAX_FMU_INPUTS`, `MAX_FMU_OUTPUTS`.
- Present detected inputs/outputs; author selects which to expose and which inputs are required. Create/update `ValidatorCatalogEntry` rows (no renaming; use sanitized FMU variable names).

3. Test case (probe)

- Collect test input JSON and expected output JSON.
- Kick off async Modal probe (HTMX progress like validation runs). While running, validator remains read-only; author may browse elsewhere. Cancel stops job and deletes draft objects.
- On success (outputs match expected with defined tolerance), mark validator/FMUModule approved, keep catalog entries.
- On failure, surface errors and allow retry on the same screen.

Cancellation at any point deletes the draft validator, FMUModel, catalog entries, probe records; no relic data is left behind.

**Runtime**

1. Resolve per-step bindings (submission/context → catalog entry slugs) to produce FMU inputs.
2. Call Modal with `(fmu_storage_key, inputs, simulation_config, output_variable_names)`.
3. Receive outputs.
4. Register outputs as signals via the catalog entries for downstream CEL.
5. Continue workflow execution.

### 2. FMU Storage and Metadata

We introduce an `FMUModel` entity:

- Stores metadata about the FMU and where it lives (e.g., S3 key).
- Tracks whether it is approved for workflow use.

Example fields:

- `project` – FK to `Project`.
- `name` – human name.
- `file` – `FileField` or S3 path, e.g. `fmi/{project_id}/{uuid}.fmu`.
- `fmi_version` – string (“2.0”, “3.0”).
- `kind` – “ModelExchange” or “CoSimulation”.
- `is_approved` – boolean indicating whether the FMU passed safety checks.
- `introspection_metadata` – JSON (e.g., list of variables, capabilities, etc.).

FMUs are uploaded through the Validibot UI, stored in a bucket, and referenced by key. The FMU file is never extracted or executed on Django/gunicorn nodes.

### 3. Automatic Introspection of Variables → Catalog

Upon upload (or via a background task), we will:

1. Open the FMU as a zip and parse `modelDescription.xml`.
2. Enumerate `ScalarVariable` elements and capture:
   - `name`
   - `causality` (input, output, parameter, local, etc.)
   - `variability`
   - `valueReference`
   - type information (Real, Integer, Boolean, String, Enumeration)
   - units (for Real variables)
3. Persist this in an `FMIVariable` model tied to `FMUModel`.

Example `FMIVariable` fields:

- `fmi_model` – FK → `FMIModel`
- `name`
- `causality`
- `variability`
- `value_reference`
- `value_type` (Real/Integer/Boolean/String/Enumeration)
- `unit`
- `catalog_entry` – optional FK → `CatalogEntry` (for IO that we expose)

We will then **offer the author a selection screen** to choose which variables should be exposed to the Validator Catalog:

- For `causality = input` → candidate input signals.
- For `causality = output` → candidate output signals.

For each selected variable, we auto-create a `CatalogEntry` (or equivalent) so it appears in the Workflow + CEL authoring experience under consistent names.

This allows workflow authors to:

- Bind inputs from submissions and other signals to FMU inputs.
- Use FMU outputs in CEL expressions as first-class signals.

### 4. Execution in Modal.com

All FMU execution happens in Modal.com. We will define one or more Modal functions with:

- A minimal Docker image including `fmpy` (or other FMI runtime) and S3 client.
- Non-root user.
- Tight resource limits (CPU, memory, wall-clock timeout).
- No outbound network access (or extremely restricted).
- No access to application secrets beyond what’s needed to fetch the FMU.

A typical function signature:

```python
run_fmu_validation(
    fmu_s3_key: str,
    inputs: dict[str, float | int | bool | str],
    simulation_config: dict,
    output_variables: list[str]
) -> dict[str, float | int | bool | str]
```

Execution flow in Modal:

1. Download FMU to `/tmp/model.fmu` from S3.
2. Perform basic integrity checks (size, zip sanity).
3. Use `fmpy` or similar to:
   - Read `modelDescription.xml` (as needed).
   - Run a simulation from `start_time` to `stop_time` with provided inputs.
4. Extract last values (or specified time slices) for `output_variables`.
5. Return outputs as a JSON-serializable dict.

The Django workflow engine will invoke this Modal function asynchronously/synchronously (depending on the existing task model), treat errors/timeouts as validation failures or internal errors, and log outcomes for observability.

### 5. Safety, Suitability, and Approval Flow

Because FMUs are untrusted native code, we will apply multiple layers of protection:

#### 5.1. Upload-Time Checks (Django)

- **Size limit**: reject FMUs above a configured threshold (e.g. 50–100 MB).
- **File type check**: ensure the file is a valid zip with `modelDescription.xml`.
- **Basic FMI version/kind check**:
  - Only accept supported FMI versions (starting with 2.0, 3.0).
  - Optionally restrict to either ModelExchange or CoSimulation depending on what the runtime supports.

FMUs that pass these checks are stored but **not yet approved** for use.

#### 5.2. Probe Run (Modal)

We will run a short “probe” simulation:

- Very small horizon (e.g., 0 → 1 s model time).
- Default or simple inputs.
- Short timeout (e.g., 5–10 seconds wall-clock).

The probe will:

- Verify that the FMU can be instantiated and stepped.
- Estimate resource usage and rough performance.
- Fail early for FMUs that crash, hang, or demand unsupported capabilities.

If the probe passes, the `FMIModel` is flagged `is_approved = True`. Otherwise, it remains unapproved and is unavailable to workflow steps (with a clear message to the author).

#### 5.3. Runtime Guards (Modal)

For each validation run:

- Enforce strict CPU, memory, and timeout limits.
- Use a “pure function” style driver: no persistent state between runs.
- Avoid mounting host volumes or exposing internal network.

If a run exceeds limits or fails, the workflow step will:

- Mark the validation step as errored.
- Capture structured logs for debugging.
- Optionally allow the org to see that “FMU X is unreliable for the selected horizon/inputs.”

### 6. Usage Semantics: Short, One-Shot Validations Only

We will explicitly scope FMI-based validation to **short, self-contained simulations** that behave roughly like functions:

> `(inputs, simulation_config) → outputs`

We **will not** support:

- Real-time co-simulation that needs tight external stepping loops aligned with wall-clock time.
- Long-running simulations meant to run for minutes or hours of real time per validation.
- FMUs that depend on external resources (files, network, external processes).

This scope will be communicated in UI copy and documentation, as well as enforced via timeouts and probe runs.

## Consequences

### Positive

- **Rich, simulation-based validation** becomes possible in Validibot using a standard (FMI).
- **Workflow authors get a clean UX**:
  - Upload FMU.
  - Pick inputs/outputs from an auto-detected list.
  - Bind to existing signals and reference results in CEL.
- **Security and isolation are improved** by never executing FMUs on the core web/app nodes and leveraging Modal’s sandboxing.
- **Architecture stays clean**:
  - FMU execution is just another external compute step, like other heavy simulations.
  - Django remains orchestration + configuration only.

### Negative / Trade-offs

- **Increased complexity and operational cost**:
  - Requires Modal infrastructure, monitoring, and cost management.
  - Adds a new external dependency (`fmpy` or equivalent).
- **Performance constraints**:
  - Some FMUs may be too slow or heavy for the one-shot, short-horizon model.
  - Authors may be confused when their existing “big” FMUs fail probe runs.
- **User education required**:
  - Clear documentation on what FMI versions/kinds are supported.
  - Guidance for building “validation-friendly” FMUs (short runs, stable behavior).

### Alternatives Considered

1. **Run FMUs directly on the Django/gunicorn/Celery infrastructure**

   - Rejected due to security risk: FMUs can contain arbitrary native code.
   - Would increase blast radius of a malicious FMU.

2. **Disallow FMI completely and only allow scripted validators (e.g. Python CEL wrappers)**

   - Simpler, but loses interoperability with existing engineering tools.
   - Many users already have FMUs; requiring rewrites would reduce adoption.

3. **Pre-approve only a curated set of FMUs (no user uploads)**

   - Safer but less flexible.
   - Could be a future “managed library” offering, but we still need user-uploads for generality.

4. **Use a different execution backend (self-managed cluster, other cloud service)**

   - Modal.com is chosen for now due to:
     - Good fit with short-lived, containerized compute.
     - Familiarity and reuse with other Validibot simulation work.
   - Backend can be revisited later if requirements or pricing change.

## Implementation Notes / Next Steps

1. **Data model**

   - Add `FMUModel` (scoped to `Project`/`Org`), FK from `Validator` (FMI type) to `FMUModel`, and store a full parsed IO snapshot (JSON) on `FMUModel` for auditing/re-rendering.
   - Add `ValidationType.FMI`.
   - Extend `ValidatorCatalogEntry` to capture FMU IO metadata using existing fields (`entry_type` SIGNAL/DERIVATION, `run_stage` INPUT/OUTPUT, `is_required`) plus a new `hidden` flag and optional `default_value` (constrained by data type) for hidden signals, and store FMI specifics (value_reference, causality, variability, value_type, unit). Use sanitized FMU variable names as-is. Catalog entries define signals; Rulesets remain for assertions only. Type metadata on catalog entries will be used by the CEL evaluator for casting and validation.
   - Keep per-step bindings (submission/signal → catalog entry slug) in step config, not Ruleset content.
   - Add a probe/test status record (e.g., `ValidatorProbeResult`) on the draft validator to store probe state/errors, and provide a Pydantic schema plus a model getter to return the FMU variable snapshot as a strongly typed object.

2. **Authoring flow (UI-only, full-page wizard)**

   - Step 1: Create FMI Validator (name/slug/description/project/org). Upload FMU file. Enqueue async probe/introspection. Show read-only state and allow “Cancel” to delete Validator, FMUModel, catalog drafts, and any queued/running Celery job.
   - Step 2: After introspection, present detected inputs/outputs. Author selects which to expose and which inputs are required. Create/update `ValidatorCatalogEntry` rows accordingly (no renaming; sanitized FMU variable names).
   - Step 3: Collect test input JSON and expected output JSON. Kick off async FMU probe on Modal (HTMX progress like validation runs). For MVP, compare outputs exactly (no tolerance); we can iterate to add numeric tolerance later. If outputs match, mark Validator/FMUModule approved and keep catalog entries; otherwise surface errors and allow retry on the same step.
   - Cancellation at any point deletes the draft Validator, FMUModel, catalog entries, and probe records; no relic data remains.
   - Org scoping/permissions: only OWNER/ADMIN/AUTHOR in the current org can run this wizard; all lookups are org-scoped.

3. **Upload & introspection**

   - Validate FMU: size/type check, presence of `modelDescription.xml`, supported FMI version/kind (MVP: FMI 2.0/3.0 Co-Simulation only).
   - Parse variables, store full parsed IO set as JSON on `FMUModel` (Pydantic-backed getter), and synthesize proposed catalog entries (inputs/outputs).
   - Apply limits: `MAX_FMU_SIZE`, `MAX_FMU_INPUTS`, `MAX_FMU_OUTPUTS`.

4. **Probe/test execution service**

   - Add a dedicated service class to run probes on the compute plane (separate from the validation-run executor, but with similar task/status shape for clarity).
   - Inputs: FMU storage key, selected variables, test input JSON, simulation config (t_start/t_end/step), timeout (`MAX_COMPUTE_TIME`), expected outputs.
   - Behavior: download FMU, run short simulation, return outputs; compare to expected (exact match for MVP); set probe status (pending/running/succeeded/failed) on the draft validator.
   - Errors/violations: fail the probe, surface clear messages via an HTMX-poll status endpoint.

5. **Step authoring (approved validator)**

   - Add FMI to the validator type selector. When selected, list the validator’s catalog entries (with flags).
   - Store per-step bindings (submission fields/other signals → input entry slugs; optionally output mapping) in the step config; assertions remain in Rulesets and reference the same catalog entry slugs.
   - Ensure executors can run steps only when the validator is approved.

6. **Constants**

   - Define in settings/constants: `MAX_FMU_SIZE`, `MAX_FMU_INPUTS`, `MAX_FMU_OUTPUTS`, `MAX_COMPUTE_TIME`, default probe horizon/step size, compute CPU/memory limits, and per-FMU simulation horizon settings (`t_start`, `t_end_default`, `max_t_end`). Enforce on upload/probe and reject author configs that exceed FMU limits.

7. **Documentation & UX**
   - Document supported FMI versions/kinds, limits, as-is naming (no renaming), probe flow, and required roles/org scoping.
   - Treat documentation as part of the deliverable: developer docs and user-facing docs with background, usage, and detailed examples. Follow AGENTS.md guidance, Google Python coding standards, and add clear code comments where they aid comprehension.
