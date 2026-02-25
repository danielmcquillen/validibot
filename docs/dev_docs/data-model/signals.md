# Signals

Signals are named values that flow through a validation run. They let validators
declare what data they consume and produce, and they let workflow authors write
assertions that reference those values by name. A signal might be an input like
"expected floor area" that the submitter provides, or an output like
"site electricity consumption" that an EnergyPlus simulation computes.

Signals are the mechanism that connects the dots between submission metadata,
validator execution, and assertion evaluation. Without them, assertions would
need to hard-code paths into raw payloads. With them, a workflow author writes
`site_eui_kwh_m2 < 100` and the platform resolves the value automatically.

## Catalog entries

Every signal is registered as a `ValidatorCatalogEntry` row belonging to a
validator. The catalog entry defines:

| Field | Purpose |
|-------|---------|
| `slug` | Stable name used in assertions and CEL expressions (e.g., `site_eui_kwh_m2`). |
| `entry_type` | `SIGNAL` (direct value) or `DERIVATION` (computed from other signals). |
| `run_stage` | `INPUT` (available before the validator runs) or `OUTPUT` (produced by the validator). |
| `data_type` | Value type: `NUMBER`, `STRING`, `BOOLEAN`, `TIMESERIES`, or `OBJECT`. |
| `target_data_path` | Path used to locate the value in the payload or processor output. |
| `binding_config` | Provider-specific hints (e.g., EnergyPlus meter name, derivation expression). |
| `is_required` | Whether the signal must be present. Missing required signals evaluate to `null`. |
| `is_hidden` | Hidden from authoring UI but still available to CEL expressions. |
| `order` | Display ordering in the UI and evaluation order. |
| `default_value` | Default value for hidden signals (JSONField, nullable). |
| `metadata` | Provider-specific metadata (e.g., `{"units": "kWh/m²"}`). |

**Uniqueness constraint**: Only one entry per `(validator, entry_type, run_stage, slug)`
tuple, enforced by the `uq_validator_catalog_entry` database constraint.

**Model location**: `validibot/validations/models.py` — class `ValidatorCatalogEntry`.

See [Validators](validators.md) for how catalog entries are defined and synced.

## Signal types

### Input signals

Input signals represent values available *before* the validator runs. They
typically come from submission metadata or form fields provided at launch time.

For example, EnergyPlus defines these input signals:

- `expected_floor_area_m2` — User-provided floor area for comparison
- `target_eui_kwh_m2` — Target energy use intensity for compliance checking
- `max_unmet_hours` — Maximum allowable unmet hours threshold

Input signals let authors write assertions against what the submitter *claims*,
before the validator even runs. An input assertion like
`expected_floor_area_m2 > 0` catches bad metadata early.

### Output signals

Output signals represent values the validator *produces* during execution. For
advanced validators (EnergyPlus, FMU), these are extracted from the container's
output envelope. For built-in validators, the validator can populate them directly.

EnergyPlus output signals include metrics like:

- `site_electricity_kwh` — Total electricity consumption
- `site_natural_gas_kwh` — Total gas consumption
- `site_eui_kwh_m2` — Energy use intensity per square meter
- `floor_area_m2` — Simulated floor area
- `unmet_heating_hours` / `unmet_cooling_hours` — Comfort metrics

Output signals are the primary mechanism for writing assertions against
simulation results. An assertion like `site_eui_kwh_m2 < target_eui_kwh_m2`
compares an output signal against an input signal in a single CEL expression.

### Derivations

Derivations are computed from other signals using expressions. They always have
`entry_type=DERIVATION` and their `binding_config` contains an `expr` field
with the computation:

```json
{
  "entry_type": "derivation",
  "run_stage": "output",
  "slug": "total_unmet_hours",
  "binding_config": {
    "expr": "unmet_heating_hours + unmet_cooling_hours"
  }
}
```

Another example:

```json
{
  "slug": "total_site_energy_kwh",
  "binding_config": {
    "expr": "(site_electricity_kwh ?? 0) + (site_natural_gas_kwh ?? 0) + (site_district_cooling_kwh ?? 0) + (site_district_heating_kwh ?? 0)"
  }
}
```

Derivations are currently gated behind the `ENABLE_DERIVED_SIGNALS` setting
(defaults to `False`). When disabled, derivation entries are excluded from CEL
context building and hidden from the authoring UI.

## How signals are defined

### Config-based definition (advanced validators)

Advanced validators define their signals in `config.py` modules co-located with
the validator code. Each config module exports a `ValidatorConfig` instance
containing a list of `CatalogEntrySpec` objects.

**Key files**:

- `validibot/validations/validators/base/config.py` — `CatalogEntrySpec` and `ValidatorConfig` Pydantic models
- `validibot/validations/validators/energyplus/config.py` — EnergyPlus signal definitions (~36 entries)
- `validibot/validations/validators/fmu/config.py` — FMU config (empty `catalog_entries`; signals created dynamically via introspection)

**CatalogEntrySpec fields**:

```python
class CatalogEntrySpec(BaseModel):
    slug: str                           # e.g., "site_eui_kwh_m2"
    label: str = ""                     # Human-friendly display name
    entry_type: str                     # "SIGNAL" or "DERIVATION"
    run_stage: str = "output"           # "INPUT" or "OUTPUT"
    data_type: str = "number"           # number, string, boolean, timeseries, object
    binding_config: dict[str, Any] = {} # Provider-specific extraction config
    metadata: dict[str, Any] = {}       # UI metadata (units, tags, etc.)
    is_required: bool = False
    order: int = 0
    description: str = ""
```

**EnergyPlus binding_config patterns**:

```python
# Input signal sourced from submission metadata
CatalogEntrySpec(
    slug="expected_floor_area_m2",
    run_stage="input",
    binding_config={"source": "submission.metadata", "path": "floor_area_m2"},
)

# Output signal sourced from EnergyPlus simulation metrics
CatalogEntrySpec(
    slug="site_eui_kwh_m2",
    run_stage="output",
    binding_config={"source": "metric", "key": "site_eui_kwh_m2"},
)

# Derivation computed from other signals
CatalogEntrySpec(
    slug="total_unmet_hours",
    entry_type="derivation",
    run_stage="output",
    binding_config={"expr": "unmet_heating_hours + unmet_cooling_hours"},
)
```

### Dynamic definition (FMU validators)

FMU validators don't predefine signals in config. Instead, when an FMU file is
uploaded, `sync_fmu_catalog()` in `validibot/validations/services/fmu.py`
introspects the FMU's `modelDescription.xml`, discovers all input/output
variables, and creates `ValidatorCatalogEntry` rows dynamically.

Each FMU variable's `causality` (input, output, parameter) determines whether
it becomes an INPUT or OUTPUT signal. The slug is derived from the variable
name via `slugify()`.

### Custom validators

Users can add signals to custom validators through the UI. The
`ValidatorCatalogEntryForm` in `validibot/validations/forms.py` handles
creation and editing.

### Syncing configs to the database

The `sync_validators` management command (`validibot/validations/management/commands/sync_validators.py`)
discovers all `ValidatorConfig` instances via `discover_configs()` and
upserts `Validator` + `ValidatorCatalogEntry` rows.

```bash
python manage.py sync_validators
```

The sync process:

1. Calls `discover_configs()` to scan validator packages for config modules
2. For each config, calls `Validator.objects.get_or_create(slug=cfg.slug)`
3. Updates validator fields from the config (name, type, flags, etc.)
4. For each `CatalogEntrySpec`, calls `ValidatorCatalogEntry.objects.get_or_create(validator, slug, entry_type)`
5. Updates catalog entry fields (binding_config, metadata, order, etc.)

## How signals flow during execution

### Complete lifecycle

```
1. DEFINITION                     config.py or UI
   └─ CatalogEntrySpec            Pydantic model

2. SYNC TO DATABASE               manage.py sync_validators
   └─ ValidatorCatalogEntry       Django model rows

3. VALIDATION RUN STARTS          StepOrchestrator.run()
   ├─ Input assertion evaluation  _build_cel_context() + evaluate_assertions_for_stage("input")
   └─ Validator execution         Container runs (EnergyPlus, FMU) or in-process

4. OUTPUT PROCESSING              AdvancedValidator.post_execute_validate()
   ├─ extract_output_signals()    Convert envelope → dict
   ├─ Output assertion evaluation _build_cel_context() + evaluate_assertions_for_stage("output")
   └─ store_signals()             Persist to run.summary

5. DOWNSTREAM STEPS               StepOrchestrator handles next step
   ├─ _extract_downstream_signals()
   ├─ Pass via RunContext.downstream_signals
   └─ Available in CEL as steps.<step_id>.signals.<slug>
```

### Within a single step

1. **CEL context building.** The validator calls `_build_cel_context()`, which
   queries the validator's catalog entries and resolves each slug against the
   payload. Input signals are resolved from submission metadata. Output signals
   are resolved from the validator's output envelope.

2. **Assertion evaluation.** CEL expressions reference signals by slug. The
   expression `site_eui_kwh_m2 < 100` looks up `site_eui_kwh_m2` in the
   context dictionary built in step 1.

3. **Signal extraction.** The validator returns extracted signals in
   `ValidationResult.signals`. The processor calls `store_signals()` to persist
   them in `validation_run.summary["steps"][step_run_id]["signals"]`.

### Across steps (cross-step communication)

Signals from earlier steps are available to later steps in the same run:

1. **Storage.** When step N completes, its signals are saved to
   `validation_run.summary`:

   ```json
   {
     "steps": {
       "42": {
         "signals": {
           "site_eui_kwh_m2": 87.5,
           "site_electricity_kwh": 12500
         }
       }
     }
   }
   ```

2. **Collection.** Before step N+1 runs, `StepOrchestrator._extract_downstream_signals()`
   reads the summary and collects signals from all prior steps.

3. **Context injection.** The collected signals are passed to the validator via
   `RunContext.downstream_signals`, then exposed in the CEL context under a
   `steps` namespace:

   ```cel
   steps.<step_run_id>.signals.<slug>
   ```

This lets a downstream step write assertions that reference outputs from an
earlier step. For example, a compliance-checking step could assert that the
EnergyPlus step's output meets a threshold.

## CEL context building in detail

The `_build_cel_context()` method on `BaseValidator` (`validibot/validations/validators/base/base.py`)
is the heart of signal resolution. It builds the dictionary that CEL expressions
evaluate against.

**Signature**:

```python
def _build_cel_context(
    self,
    payload: Any,
    validator: Validator,
) -> dict[str, Any]
```

**What it does**:

1. **Initializes the context** with `{"payload": payload}` so CEL expressions
   can access raw data via the `payload` variable.

2. **Iterates catalog entries** ordered by `(order, pk)`. For each entry, calls
   `_resolve_path(payload, entry.slug)` to extract the value from the payload.
   If found, adds it to the context under the slug key.

3. **Handles stage prefixing.** When both INPUT and OUTPUT stages define the same
   slug, output signals are namespaced as `output.<slug>` to avoid collisions.

4. **Injects downstream signals.** If the `RunContext` includes `downstream_signals`
   from prior steps, they are added to the context under `steps`:

   ```python
   context["steps"] = {
       "<step_run_id>": {
           "signals": {"site_eui_kwh_m2": 75.2, ...}
       }
   }
   ```

5. **Custom assertion targets.** For validators with `allow_custom_assertion_targets=True`,
   top-level payload keys are exposed directly to CEL (allows assertions against
   arbitrary fields without predefined signals).

**Signal availability in CEL expressions**:

| Expression | Source |
|-----------|--------|
| `site_eui_kwh_m2` | Direct catalog signal (INPUT or OUTPUT) |
| `output.site_eui_kwh_m2` | Prefixed output signal (when slug conflicts with input) |
| `steps["42"].signals.site_eui_kwh_m2` | Cross-step signal from step run ID 42 |
| `payload` | Raw submission/envelope data |
| `payload.results.energy` | Direct access to raw payload fields |

## Path resolution

Two parallel `_resolve_path()` implementations exist in the codebase:

1. **`BaseValidator._resolve_path()`** (`validibot/validations/validators/base/base.py`)
   — Used for CEL context building. Handles dotted paths and bracket notation
   for array indexing. Returns `(value, found)` tuple.

2. **`BasicAssertionEvaluator._resolve_path()`** (`validibot/validations/assertions/evaluators/basic.py`)
   — Used for BASIC operator evaluation. Same dotted-path and bracket notation
   support, implemented with regex tokenization.

Both support the same syntax:

- Dotted paths: `building.envelope.wall.u_value`
- Bracket notation: `results[0].temp`
- Mixed: `building.floors[0].zones[1].sensors[2]`

A third resolver, `resolve_input_value()` in `validibot/validations/services/fmu_bindings.py`,
handles FMU input binding resolution. It takes a `data_path` and `slug`; if
`data_path` is empty, it falls back to using the slug as a top-level key lookup.

Comprehensive tests for all three resolvers are in
`validibot/validations/tests/test_resolve_path.py`.

## Signal extraction for advanced validators

Advanced validators (EnergyPlus, FMU) run in Docker containers and return their
results in an output envelope. The validator class defines an
`extract_output_signals()` class method that converts the envelope into a flat
signal dictionary.

### EnergyPlus extraction

**File**: `validibot/validations/validators/energyplus/validator.py`

```python
@classmethod
def extract_output_signals(cls, output_envelope: Any) -> dict[str, Any] | None:
    metrics = output_envelope.outputs.metrics
    if hasattr(metrics, "model_dump"):
        metrics_dict = metrics.model_dump(mode="json")
        return {k: v for k, v in metrics_dict.items() if v is not None}
```

The `EnergyPlusSimulationMetrics` Pydantic model (from `validibot-shared`)
defines all possible output fields. `model_dump()` converts them to a dict,
and `None` values are filtered out. The resulting dict maps signal slugs to
their values:

```python
{
    "site_eui_kwh_m2": 75.2,
    "site_electricity_kwh": 12345.0,
    "floor_area_m2": 10000.0,
    "zone_count": 42,
    ...
}
```

The signal slugs in this dict must match the `slug` field on the corresponding
`ValidatorCatalogEntry` rows, and those slugs must match the field names on
`EnergyPlusSimulationMetrics`. This is how the catalog entries, config specs,
and shared library models stay in sync.

### FMU input resolution

**File**: `validibot/validations/services/cloud_run/launcher.py`

Before launching an FMU container, the launcher resolves input signals from
the submission payload:

```python
for entry in validator.catalog_entries.filter(run_stage="INPUT"):
    slug = entry.slug
    value = resolve_input_value(
        submission_payload,
        data_path=(entry.target_data_path or "").strip(),
        slug=slug,
    )
    if value is None and entry.is_required:
        raise ValueError(f"Missing required input '{slug}' for FMU validator.")
    if value is not None:
        input_values[slug] = value
```

The `target_data_path` field on the catalog entry tells the resolver where to
find the value. If `target_data_path` is empty, the resolver falls back to
using the slug as a top-level key in the submission payload.

## Assertion evaluation with signals

Assertions evaluate against the CEL context that signals populate. The key
classes involved:

**`AssertionContext`** (`validibot/validations/assertions/evaluators/base.py`):

```python
@dataclass
class AssertionContext:
    validator: Validator
    engine: BaseValidator
    cel_context: dict[str, Any] | None = None

    def get_cel_context(self, payload: Any) -> dict[str, Any]:
        if self.cel_context is None:
            self.cel_context = self.engine._build_cel_context(payload, self.validator)
        return self.cel_context
```

The CEL context is built lazily on first access and cached for reuse across
multiple assertions in the same evaluation pass.

**`evaluate_assertions_for_stage()`** (`validibot/validations/validators/base/base.py`):

This is the unified entry point for assertion evaluation. It:

1. Merges assertions from two sources: `validator.default_ruleset` (always runs)
   and the step-level `ruleset` (per-workflow assertions)
2. Filters assertions by `resolved_run_stage` matching the current stage
3. Builds a single `AssertionContext` (CEL context lazy-built once)
4. Evaluates all matching assertions via their type-specific evaluator
5. Returns `AssertionEvaluationResult` with issues, totals, and failure counts

## Storage

Signals are not stored in a dedicated table. They live in the `summary` JSONField
on `ValidationRun`, nested under `steps.<step_run_id>.signals`. This keeps
signal storage lightweight (no extra rows per signal per run) and naturally
scoped to the run lifecycle.

The `store_signals()` method on `ValidationStepProcessor`
(`validibot/validations/services/step_processor/base.py`) handles persistence:

```python
def store_signals(self, signals: dict[str, Any]) -> None:
    if not signals:
        return
    summary = self.validation_run.summary or {}
    steps = summary.setdefault("steps", {})
    step_key = str(self.step_run.id)
    step_data = steps.setdefault(step_key, {})
    step_data["signals"] = signals
    self.validation_run.summary = summary
    self.validation_run.save(update_fields=["summary"])
```

The `_extract_downstream_signals()` method on `StepOrchestrator`
(`validibot/validations/services/step_orchestrator.py`) reads these
stored signals back for downstream steps:

```python
def _extract_downstream_signals(
    self, validation_run: ValidationRun | None,
) -> dict[str, Any]:
    summary = getattr(validation_run, "summary", None) or {}
    steps = summary.get("steps", {}) or {}
    scoped_signals: dict[str, Any] = {}
    for key, value in steps.items():
        if isinstance(value, dict):
            scoped_signals[str(key)] = {
                "signals": value.get("signals", {}) or {}
            }
    return scoped_signals
```

## Key function reference

| Function | File | Purpose |
|----------|------|---------|
| `discover_configs()` | `validators/base/config.py` | Scans validator packages for config modules |
| `sync_validators` | `management/commands/sync_validators.py` | Management command to sync configs to DB |
| `_build_cel_context()` | `validators/base/base.py` | Builds signal dict for CEL evaluation |
| `_resolve_path()` | `validators/base/base.py` | Resolves dotted/bracket paths in data |
| `resolve_input_value()` | `services/fmu_bindings.py` | Resolves FMU input values from submission |
| `store_signals()` | `services/step_processor/base.py` | Persists signals to run summary |
| `_extract_downstream_signals()` | `services/step_orchestrator.py` | Collects signals from prior steps |
| `evaluate_assertions_for_stage()` | `validators/base/base.py` | Evaluates assertions against signal context |
| `extract_output_signals()` | `validators/energyplus/validator.py` | Extracts signals from EnergyPlus output |
| `sync_fmu_catalog()` | `services/fmu.py` | Creates FMU signals from model introspection |

## Related documentation

- [Validators](validators.md) — Catalog entry model and seed data
- [Assertions](assertions.md) — How signals are referenced in rules
- [Step Processor](../overview/step_processor.md) — Signal extraction and storage implementation
- [Results](results.md) — How signal values appear in run summaries
