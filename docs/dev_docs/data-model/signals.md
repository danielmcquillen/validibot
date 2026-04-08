# Signals

Signals are named values that flow through a validation run. They let workflow
authors write assertions that reference data by name rather than hard-coded
paths. A signal might be a mapped submission value like "expected floor area"
that the author names at the workflow level, a validator input like "wall R
value" that comes from a step's binding configuration, or a validator output
like "site electricity consumption" that an EnergyPlus simulation produces.

Signals are the mechanism that connects the dots between submission metadata,
validator execution, and assertion evaluation. Without them, assertions would
need to hard-code paths into raw payloads. With them, a workflow author writes
`s.site_eui_kwh_m2 < 100` and the platform resolves the value automatically.

For a concrete worked example, see
[Signals Tutorial Example](signals-tutorial-example.md).


## Three concepts: Signals vs Validator Inputs vs Validator Outputs

Understanding the signal system requires distinguishing three related but
different concepts. Each occupies a different namespace in CEL expressions and
is managed by a different model.

### Signals (the `s` namespace)

Signals are author-defined named values available to every step in a workflow.
They come from two sources:

1. **Workflow-level signal mappings** (`WorkflowSignalMapping`) -- the author
   names a value and maps it to a path in the submission data. Resolved once
   before any step runs.
2. **Promoted validator outputs** (`SignalDefinition.signal_name`) -- the
   author promotes a specific output from an earlier step into the signal
   namespace. The value becomes available to downstream steps.

In CEL expressions, signals are accessed as `s.<name>` or `signal.<name>`.

### Validator inputs (the `s` namespace, step-level)

Validator inputs are values a step expects to receive. They are declared as
`SignalDefinition` rows with `direction=INPUT` and are resolved from the
submission data via `StepSignalBinding` rows or the signal's `contract_key`.
Input signals are injected into the `s` namespace alongside workflow-level
signals, but workflow-level signals take precedence if there is a name
collision.

### Validator outputs (the `o` / `output` namespace)

Validator outputs are values a step produces during execution. They are
declared as `SignalDefinition` rows with `direction=OUTPUT`. After the
validator runs, the output payload is placed in the `o` / `output` namespace
so assertions can reference values as `o.<name>` or `output.<name>`.

Outputs can optionally be **promoted** to the signal namespace by setting
`signal_name` on the `SignalDefinition`. This makes the output value available
as `s.<signal_name>` in all downstream steps.

### Summary table

| Concept | CEL namespace | Model | Scope |
|---------|:-------------|:------|:------|
| Workflow signals | `s.<name>` / `signal.<name>` | `WorkflowSignalMapping` | All steps |
| Promoted outputs | `s.<signal_name>` / `signal.<signal_name>` | `SignalDefinition` (with `signal_name`) | Downstream steps |
| Validator inputs | `s.<contract_key>` (step-level) | `SignalDefinition` (direction=INPUT) | Current step |
| Validator outputs | `o.<contract_key>` / `output.<contract_key>` | `SignalDefinition` (direction=OUTPUT) | Current step |
| Cross-step outputs | `steps.<step_key>.output.<name>` | Run summary storage | Downstream steps |
| Raw payload | `p.<path>` / `payload.<path>` | (none -- raw data) | Current step |


## The CEL context structure

Every CEL expression evaluates against a context with four namespaces and
two aliases. The context is built by `_build_cel_context()` in
`validibot/validations/validators/base/base.py`.

```python
context = {
    "p": payload,            # alias for payload
    "payload": payload,      # raw submission or validator output data
    "s": signals_dict,       # alias for signal
    "signal": signals_dict,  # workflow signals + promoted outputs + step inputs
    "o": output_dict,        # alias for output
    "output": output_dict,   # this step's declared output signals
    "steps": steps_context,  # outputs from completed upstream steps
}
```

### `p` / `payload` -- raw submission data

Always present. Contains the raw submission data (for input-stage assertions)
or the validator's output envelope (for output-stage assertions). Authors
access raw fields via `p.building.envelope.wall_r_value` or
`payload.results[0].value`.

### `s` / `signal` -- author-defined signals

Contains the merged signal namespace built from three sources (in priority
order):

1. **Workflow-level signals** from `RunContext.workflow_signals` (resolved
   from `WorkflowSignalMapping` rows)
2. **Promoted validator outputs** from `SignalDefinition` rows with non-empty
   `signal_name` (injected by `_inject_promoted_outputs()`)
3. **Step-bound input signals** from `StepSignalBinding` rows (resolved from
   submission data, only during input stage)

Workflow-level signals take precedence over step-level bindings with the same
name. Authors access signals via `s.target_eui` or `signal.target_eui`.

### `o` / `output` -- validator output signals

For output-stage assertions, this contains the full validator output payload
(the dict produced by the validator). For input-stage assertions, declared
output signals are resolved from the payload so `output.name` is available
even before the validator runs (useful for cross-direction comparisons).

Authors access output values via `o.site_eui_kwh_m2` or
`output.site_eui_kwh_m2`.

### `steps` -- cross-step outputs

Contains validator outputs from completed upstream steps. Each entry is keyed
by the step's `step_key` (a stable slug set on `WorkflowStep`) and contains
an `output` dict with the step's extracted signal values.

Authors access cross-step values via `steps.envelope_check.output.floor_area_m2`.

```json
{
  "envelope_check": {
    "output": {
      "floor_area_m2": 10000.0,
      "site_eui_kwh_m2": 75.2
    }
  }
}
```

### CEL expression examples

```cel
# Workflow signal (mapped from submission data)
s.target_eui < 100

# Promoted output from a prior step
s.simulated_eui < s.target_eui

# This step's output
output.T_room < 300.15

# Raw payload access
p.building.envelope.wall_r_value > 10

# Cross-step output
steps.energyplus_step.output.site_eui_kwh_m2 < 100

# Null guard for optional signals
s.max_unmet_hours != null && output.unmet_hours < s.max_unmet_hours
```


## Model: `WorkflowSignalMapping`

**File**: `validibot/workflows/models.py`

Defines a workflow-level signal -- an author's named vocabulary for a data
point in the submission payload. Each row maps a signal name to a source path
in the submission data. These signals are resolved once before any step runs
and are available to all steps in the workflow.

### Fields

| Field | Type | Purpose |
|-------|------|---------|
| `workflow` | FK to `Workflow` | The workflow that owns this mapping. |
| `name` | `CharField(100)` | Signal name. Must be a valid CEL identifier. Used as `s.<name>`. |
| `source_path` | `CharField(500)` | Data path resolved against the submission payload. |
| `default_value` | `JSONField` (nullable) | Fallback value when the source path resolves to nothing. |
| `on_missing` | `CharField(10)` | Behavior when resolution fails: `"error"` (default) or `"null"`. |
| `data_type` | `CharField(20)` | Expected type hint: `number`, `string`, `boolean`, or empty (infer). |
| `position` | `PositiveIntegerField` | Display order in the signal mapping editor. |

### Constraints

- **`unique_signal_name_per_workflow`**: One signal name per workflow,
  enforced at the database level.

### `on_missing` behavior

- **`error`** (default): The validation run fails immediately with a clear
  error message before any step is attempted.
- **`null`**: The signal is injected as `null`. The author must guard with
  `s.name != null` in CEL expressions. Accessing a null signal without a
  guard produces a fail-fast evaluation error with guidance on how to fix it.

### Example

A workflow that validates energy models might define:

| name | source_path | on_missing |
|------|-------------|:----------:|
| `target_eui` | `metadata.target_eui_kwh_m2` | error |
| `building_type` | `metadata.building_type` | null |
| `floor_area` | `building.gross_floor_area_m2` | error |

All three signals become available as `s.target_eui`, `s.building_type`, and
`s.floor_area` in every step's CEL expressions.


## Model: `SignalDefinition`

**File**: `validibot/validations/models.py`

The stable data contract for a named signal at the validator or step level.
A `SignalDefinition` declares that a validator or workflow step expects (input)
or produces (output) a named data point with a specific type. It is the
"what" -- the contract -- not the "where" (that is the binding).

This model unifies signal metadata that was previously scattered across three
legacy storage formats (`ValidatorCatalogEntry`, FMU config JSON, template
config JSON) into a single relational table.

### Key concepts

**`contract_key` vs `native_name`**: `contract_key` is the stable,
slug-safe identifier used in CEL expressions, the API, and data path bindings
(e.g., `panel_area`). `native_name` preserves the provider's original name
verbatim (e.g., an FMU's `Panel.Area_m2` or an EnergyPlus template variable
`#{heating_setpoint}`). The `contract_key` is what Validibot uses; the
`native_name` is what the provider uses.

**Ownership (XOR constraint)**: Each signal is owned by exactly one of:
- A `Validator` -- shared signal definitions that apply to every step using
  that validator (library validators).
- A `WorkflowStep` -- per-step signal definitions for step-level FMU uploads,
  template scans, or author-customized signals.

This is enforced by the `ck_sigdef_one_owner` database constraint.

**Output promotion via `signal_name`**: When an output-direction
`SignalDefinition` has a non-empty `signal_name`, the output value is
promoted to the `s` (signal) namespace in CEL expressions for all downstream
steps. This is how a validator output from one step becomes a named signal
that later steps can reference as `s.<signal_name>`.

### Fields

| Field | Type | Purpose |
|-------|------|---------|
| `contract_key` | `SlugField(255)` | Stable slug identifier used in CEL, API, and bindings. |
| `native_name` | `CharField(500)` | Provider's original name, preserved verbatim. |
| `label` | `CharField(255)` | Human-readable display label. |
| `description` | `TextField` | Detailed description. |
| `direction` | `CharField(10)` | `INPUT` or `OUTPUT` (from `SignalDirection` choices). |
| `data_type` | `CharField(20)` | Value type: `NUMBER`, `STRING`, `BOOLEAN`, `TIMESERIES`, `OBJECT`. |
| `origin_kind` | `CharField(20)` | How created: from config declaration, FMU probe, or template scan. |
| `source_kind` | `CharField(20)` | How the value is obtained: `PAYLOAD_PATH` or `INTERNAL` (see below). |
| `is_path_editable` | `BooleanField` | Whether the workflow author can edit the source data path in the step binding. |
| `validator` | FK to `Validator` (nullable) | Owner for library validators. XOR with `workflow_step`. |
| `workflow_step` | FK to `WorkflowStep` (nullable) | Owner for step-level signals. XOR with `validator`. |
| `order` | `PositiveIntegerField` | Display ordering within the owner's signal list. |
| `is_hidden` | `BooleanField` | Hidden from the default signals UI. |
| `unit` | `CharField(50)` | Unit of measurement (e.g., `kW`, `m2`, `degC`). |
| `provider_binding` | `JSONField` | Validator-type-specific binding properties (see below). |
| `metadata` | `JSONField` | Arbitrary metadata for extensions and integrations. |
| `signal_name` | `CharField(100)` | Output promotion name. When set, value is available as `s.<signal_name>`. |

### Constraints

| Constraint | Fields | Purpose |
|------------|--------|---------|
| `ck_sigdef_one_owner` | `validator`, `workflow_step` | Exactly one owner (XOR). |
| `uq_sigdef_validator_key_dir` | `validator`, `contract_key`, `direction` | Unique per validator. |
| `uq_sigdef_step_key_dir` | `workflow_step`, `contract_key`, `direction` | Unique per step. |

### `provider_binding` examples

FMU signals store causality and value reference:

```json
{
  "causality": "output",
  "value_reference": 42,
  "variability": "continuous"
}
```

EnergyPlus template signals store variable type and constraints:

```json
{
  "variable_type": "numeric",
  "min": 0,
  "max": 50,
  "choices": null
}
```

### Signal source kinds

The `source_kind` field declares how the signal's value is obtained. This
distinction is surfaced in the UI so workflow authors know which signals they
can configure and which are fixed by the validator.

**`PAYLOAD_PATH`** (default): The signal's value comes from a known data path
in the submission payload or metadata. The workflow author may (depending on
`is_path_editable`) configure the exact path via the step's signal binding.
Most FMU input signals and template signals use this mode -- the author wires
each input to the right field in their submission data.

**`INTERNAL`**: The validator has its own mechanism for extracting or computing
the value. Examples include EnergyPlus simulation metrics (extracted from the
output envelope), THERM signals (parsed from XML), and FMU output variables
(read from the FMU runtime). The source path in the step binding is typically
fixed and should not be changed by the author.

**`is_path_editable`** controls whether the source data path field in the
signal edit modal is enabled or disabled. When `False`, Django's
`field.disabled = True` provides server-side protection -- even if someone
tampers with the form HTML, Django ignores the submitted value.

| Validator | Direction | `source_kind` | `is_path_editable` |
|-----------|-----------|:-------------|:-------------------|
| EnergyPlus | Input | `INTERNAL` | `False` |
| EnergyPlus | Output | `INTERNAL` | `False` |
| THERM | Output | `INTERNAL` | `False` |
| FMU | Input | `PAYLOAD_PATH` | `True` |
| FMU | Output | `INTERNAL` | `False` |
| Template | Input | `PAYLOAD_PATH` | `True` |
| Custom | Any | `PAYLOAD_PATH` | `True` |


### Typed metadata accessors

`SignalDefinition` provides typed access to provider-specific metadata
through Pydantic accessor properties:

- `sig.fmu_binding` -- `FMUProviderBinding` (causality, value_reference, etc.)
- `sig.fmu_metadata` -- `FMUSignalMetadata` (display hints)
- `sig.template_metadata` -- `TemplateSignalMetadata` (variable type, constraints)


## How the two models relate

`WorkflowSignalMapping` and `SignalDefinition` serve different roles in the
signal architecture, but they share the same `s` namespace in CEL expressions.

```
WorkflowSignalMapping                 SignalDefinition
(workflow-level)                      (validator/step-level)

name: "target_eui"                    contract_key: "site_eui_kwh_m2"
source_path: "metadata.target_eui"    direction: OUTPUT
                                      signal_name: "simulated_eui"
        │                                      │
        ▼                                      ▼
   s.target_eui                          s.simulated_eui
        │                                      │
        └──────────── CEL ─────────────────────┘
                      │
        s.simulated_eui < s.target_eui
```

**WorkflowSignalMapping** creates signals by extracting values from submission
data. These are resolved once before any step runs and are available
everywhere.

**SignalDefinition** declares the inputs and outputs of individual validators
and steps. Outputs with a non-empty `signal_name` are "promoted" into the
signal namespace, making their values available as `s.<signal_name>` in
downstream steps.


## Cross-table signal name uniqueness

Signal names must be unique within a workflow across both models. A workflow
cannot have a `WorkflowSignalMapping` named `floor_area` and a promoted
output `SignalDefinition` with `signal_name="floor_area"` in the same
workflow.

This is enforced at the application level by `validate_signal_name_unique()`
in `validibot/validations/services/signal_resolution.py`. The function
queries both tables:

1. Checks `WorkflowSignalMapping.objects.filter(workflow_id=..., name=...)`
2. Checks `SignalDefinition.objects.filter(workflow_step__workflow_id=..., signal_name=...)`

Both models call this function in their `clean()` methods. Additionally,
`validate_signal_name()` in the same module checks that names are valid CEL
identifiers and not reserved words. The reserved names list includes all
CEL context keys (`p`, `payload`, `s`, `signal`, `o`, `output`, `steps`),
CEL built-in functions, and CEL keywords.


## Signals vs custom data paths

Assertions in Validibot target data in one of two ways, and understanding this
distinction is fundamental to how the platform works.

### Declared signals (the data contract)

When a validator author defines signals, they are publishing a **data contract**:
"this validator knows about these specific data points." Signals have names
(slugs), types, stages (input or output), and metadata. They appear in
dropdowns, support type-appropriate operators, and enable compile-time
validation of CEL expressions.

This is the structured, guided path. The validator author has done the work of
mapping data paths to meaningful names, and workflow authors benefit from that
investment.

Examples of validators with declared signals:

- **EnergyPlus** declares ~36 signals (floor area, site EUI, unmet hours, etc.)
- **FMU** auto-discovers signals by introspecting the model's variables
- **Custom validators** where the author manually adds signals through the UI

### Custom data paths (no contract)

Some validators don't declare signals. The Basic validator, JSON Schema
validator, and XML Schema validator validate structure but don't pre-declare
what specific fields exist in the data. When a workflow author uses one of
these validators and wants to write assertions, they reference data using
**custom data paths** -- dot-notation expressions accessed via the `p`
(payload) namespace, like `p.building.thermostat.setpoint` or
`p.results[0].value`.

This is the flexible, exploratory path. The workflow author navigates the
data shape themselves, without the guardrails that declared signals provide.

### How the two modes interact

The `allow_custom_assertion_targets` flag on `Validator` controls whether
workflow authors can go beyond declared signals:

| Scenario | Signals exist? | Custom paths allowed? | What the author sees |
|----------|:-:|:-:|------|
| EnergyPlus | Yes (36+) | No | Signal dropdown only |
| Custom validator with signals | Yes | Configurable | Dropdown + optional free-form paths |
| Basic validator | No | Yes (always) | Free-form path entry only |
| JSON Schema / XML Schema | No | Yes | Free-form path entry only |

When both modes are available, the form shows "Target Signal or Path" and
attempts to match user input against signal definitions first, falling back to
treating it as a custom path.


## Signal resolution: `resolve_workflow_signals()`

**File**: `validibot/validations/services/signal_resolution.py`

This is the pre-step resolution phase. Before any workflow step executes, all
`WorkflowSignalMapping` rows are resolved against the submission payload. The
result is stored in `RunContext.workflow_signals` and injected into the CEL
context as the `s` / `signal` namespace.

### Resolution algorithm

1. Query `WorkflowSignalMapping` rows for the workflow, ordered by `position`.
2. For each mapping, call `resolve_path(submission_data, mapping.source_path)`.
3. If the path resolves, store `mapping.name -> value`.
4. If not found and `default_value` is set, use the default.
5. If not found and `on_missing == "null"`, inject `None`.
6. If not found and `on_missing == "error"`, record an error.
7. If any errors accumulated, raise `SignalResolutionError`.

### Where resolution is called

`StepOrchestrator._resolve_workflow_signals()` calls
`resolve_workflow_signals()` before each step execution. The resolved dict is
passed via `RunContext.workflow_signals` to the validator, which injects it
into the CEL context.


## Promoted output reconstruction: `_inject_promoted_outputs()`

**File**: `validibot/validations/validators/base/base.py`

When a `SignalDefinition` with `direction=OUTPUT` has a non-empty
`signal_name`, the output value from the producing step is "promoted" into
the `s` namespace for all downstream steps.

### How it works

1. `_inject_promoted_outputs()` runs inside `_build_cel_context()` when the
   `steps` context is non-empty (i.e., there are completed upstream steps).
2. It queries `SignalDefinition` rows with non-empty `signal_name` across all
   steps in the current workflow.
3. For each promoted signal, it looks up the producing step's `step_key` in
   the `steps` context.
4. It extracts the output value using the signal's `contract_key` from the
   step's output dict.
5. If found, it injects the value into `signals_dict` under the promoted
   `signal_name`.

### Why it runs on every step

Promoted outputs are only available after the producing step completes. Since
different steps may complete at different times (especially with async
validators), `_inject_promoted_outputs()` runs fresh on every step rather
than once at the start of the run.

### Example

Given a `SignalDefinition`:
- `contract_key = "site_eui_kwh_m2"`, `direction = OUTPUT`,
  `signal_name = "simulated_eui"`, on step with `step_key = "energyplus_step"`

And a run summary:
```json
{"steps": {"energyplus_step": {"output": {"site_eui_kwh_m2": 75.2}}}}
```

The promoted output injects `signals_dict["simulated_eui"] = 75.2`, making
it accessible as `s.simulated_eui` in downstream CEL expressions.


## How signals are defined

### Config-based definition (advanced validators)

Advanced validators define their signals in `config.py` modules co-located with
the validator code. Each config module exports a `ValidatorConfig` instance
containing a list of `CatalogEntrySpec` objects that seed `SignalDefinition`
rows. Each `CatalogEntrySpec` can declare `source_kind` and `is_path_editable`
to control how the signal's value is obtained and whether the author can
change the source path (see [Signal source kinds](#signal-source-kinds)).

**Key files**:

- `validibot/validations/validators/base/config.py` -- `CatalogEntrySpec` and `ValidatorConfig` Pydantic models
- `validibot/validations/validators/energyplus/config.py` -- EnergyPlus signal definitions (~36 entries)
- `validibot/validations/validators/fmu/config.py` -- FMU config (empty `catalog_entries`; signals created dynamically via introspection)

### Dynamic definition (FMU validators)

FMU validators don't predefine signals in config. Instead, when an FMU file is
uploaded, `sync_fmu_catalog()` in `validibot/validations/services/fmu.py`
introspects the FMU's `modelDescription.xml`, discovers all input/output
variables, and creates `SignalDefinition` rows dynamically.

Each FMU variable's `causality` (input, output, parameter) determines whether
it becomes an INPUT or OUTPUT signal. The `contract_key` is derived from the
variable name via `slugify()`, and the `native_name` preserves the original
FMU variable name.

### Custom validators

Users can add signals to custom validators through the UI. The signal
definition forms handle creation and editing.

### Syncing configs to the database

The `sync_validators` management command
(`validibot/validations/management/commands/sync_validators.py`)
discovers all `ValidatorConfig` instances via `discover_configs()` and
upserts `Validator` + `SignalDefinition` rows.

```bash
python manage.py sync_validators
```


## How signals flow during execution

### Complete lifecycle

```
1. DEFINITION                     config.py, FMU introspection, or UI
   +- SignalDefinition            Django model rows (validator or step owned)
   +- WorkflowSignalMapping       Django model rows (workflow-level)

2. WORKFLOW RUN STARTS            StepOrchestrator.execute_workflow_steps()
   +- resolve_workflow_signals()  Resolve WorkflowSignalMapping -> s namespace
   +- _extract_downstream_signals()  Collect prior step outputs -> steps namespace

3. EACH STEP EXECUTES             _build_cel_context()
   +- Input assertion evaluation  s + p + output namespaces populated
   +- Validator execution         Container or in-process
   +- _inject_promoted_outputs()  Promote output signal_name -> s namespace
   +- Output assertion evaluation o/output namespace populated from results

4. OUTPUT STORAGE                  store_signals()
   +- run.summary["steps"][step_key]["output"] = signals
   +- Available as steps.<step_key>.output.<name> for downstream steps
```

### Within a single step

1. **CEL context building.** `_build_cel_context()` assembles the four
   namespaces: `p`/`payload` (raw data), `s`/`signal` (workflow signals +
   promoted outputs + step inputs), `o`/`output` (declared output signals),
   and `steps` (upstream step outputs).

2. **Input assertion evaluation.** CEL expressions reference signals via the
   `s` namespace and raw data via `p`. For example,
   `s.expected_floor_area > 0` checks a mapped submission value.

3. **Validator execution.** The validator runs (container launch, in-process
   check, AI call, etc.).

4. **Output extraction.** The validator returns extracted outputs. For
   advanced validators, `extract_output_signals()` converts the container
   output envelope into a flat dict.

5. **Output assertion evaluation.** CEL expressions reference output values
   via `o.site_eui_kwh_m2` or `output.site_eui_kwh_m2`.

6. **Signal storage.** `store_signals()` persists the output dict to
   `run.summary["steps"][step_key]["output"]`.

### Across steps (cross-step communication)

Signals from earlier steps are available to later steps in the same run:

1. **Storage.** When step N completes, its outputs are saved to
   `validation_run.summary`:

   ```json
   {
     "steps": {
       "energyplus_step": {
         "output": {
           "site_eui_kwh_m2": 87.5,
           "site_electricity_kwh": 12500
         }
       }
     }
   }
   ```

2. **Collection.** Before step N+1 runs,
   `StepOrchestrator._extract_downstream_signals()` reads the summary and
   collects outputs from all prior steps.

3. **Context injection.** The collected outputs are passed to the validator via
   `RunContext.downstream_signals`, then exposed in the CEL context under the
   `steps` namespace:

   ```cel
   steps.energyplus_step.output.site_eui_kwh_m2
   ```

4. **Promoted outputs.** If step N has a `SignalDefinition` with
   `signal_name="simulated_eui"`, `_inject_promoted_outputs()` places the
   value into the `s` namespace:

   ```cel
   s.simulated_eui < s.target_eui
   ```

This lets a downstream step write assertions that reference outputs from an
earlier step either by the full path (`steps.<key>.output.<name>`) or by
promoted signal name (`s.<signal_name>`).


## CEL context building in detail

The `_build_cel_context()` method on `BaseValidator`
(`validibot/validations/validators/base/base.py`) is the heart of signal
resolution. It builds the dictionary that CEL expressions evaluate against.

**Signature**:

```python
def _build_cel_context(
    self,
    payload: Any,
    validator: Validator,
    *,
    stage: str = "input",
) -> dict[str, Any]
```

**What it does**:

1. **Builds the `s` (signals) namespace** from three sources:
   - Workflow-level signals from `RunContext.workflow_signals`
   - Step-bound input signals resolved via `_resolve_bound_input_context()`
     (input stage only; workflow signals take precedence over step bindings)
   - Declared input signal defaults (ensures every declared input exists
     in the namespace, even if unresolved, to avoid undefined-variable
     CEL errors)

2. **Builds the `o` / `output` namespace.** At the output stage, the full
   validator output payload is used. At the input stage, declared output
   signals are resolved from the payload.

3. **Builds the `steps` namespace** from `RunContext.downstream_signals` or
   the run summary.

4. **Injects promoted outputs** via `_inject_promoted_outputs()` into the
   signals namespace.

5. **Assembles the final context** with all six keys (`p`, `payload`, `s`,
   `signal`, `o`, `output`, `steps`). All roots are always present (even if
   empty) so CEL expressions can reference them without undefined-variable
   errors.


## Output signal elevation pipeline

Output signals from advanced validators (FMU, EnergyPlus) go through a
multi-stage pipeline before they become CEL variables.

### Stage 1: Extraction -- `extract_output_signals()`

Each advanced validator class defines a `extract_output_signals()` classmethod
that converts the container's output envelope into a flat Python dict of
signal names to values. For FMU validators, this dict contains the final
time-step values of each output variable:

```python
# FMU extract_output_signals() returns:
{"T_room": 296.63, "Q_cooling_actual": 5172.83}
```

This method is called in `AdvancedValidator.post_execute_validate()` after
the container completes. The extracted dict is stored in
`ValidationResult.signals` and later persisted to `run.summary` by the
processor's `store_signals()` method.

### Stage 2: Payload merging

Before output-stage assertions are evaluated, the submission's input data is
merged with the extracted output signals. The output dict becomes the `o` /
`output` namespace directly.

### Stage 3: CEL context building

The merged payload flows into `_build_cel_context()`, which places the
output dict in the `o` / `output` namespace. The `s` / `signal` namespace
contains workflow signals, promoted outputs, and step-bound inputs.

### Stage 4: CEL evaluation

When cel-python compiles the expression `o.T_room < 300.15`, it parses
the dot as **member access** -- the standard CEL operator for selecting a
field from a map. At evaluation time:

1. CEL looks up the variable `o` in the activation context
2. Finds the Python dict `{"T_room": 296.63, ...}`
3. cel-python's `json_to_cel()` converts the dict to a CEL `MapType`
4. The `.T_room` selector retrieves the value `296.63` from the map
5. The comparison `296.63 < 300.15` evaluates to `true`

This is standard CEL -- no custom operators, no dialect extensions.


## Signal extraction for advanced validators

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
and `None` values are filtered out.

Not every signal will be populated for every IDF. The signal definitions
declare the full set of metrics that the validator knows how to extract, but
EnergyPlus only produces a value when the IDF is configured to generate it.
When a signal is absent from the extracted dict, the display layer reports
"Value not found".

### FMU input resolution

**File**: `validibot/validations/services/cloud_run/launcher.py`

Before launching an FMU container, the launcher resolves input signals from
the submission payload using `SignalDefinition` rows with `direction=INPUT`.


## Storage

Signals are not stored in a dedicated table. They live in the `summary`
JSONField on `ValidationRun`, nested under
`steps.<step_key>.output`. This keeps signal storage lightweight
(no extra rows per signal per run) and naturally scoped to the run lifecycle.

The `store_signals()` method on `ValidationStepProcessor`
(`validibot/validations/services/step_processor/base.py`) handles persistence:

```python
def store_signals(self, signals: dict[str, Any]) -> None:
    if not signals:
        return
    summary = self.validation_run.summary or {}
    steps = summary.setdefault("steps", {})
    step_key = self.step_run.workflow_step.step_key or str(self.step_run.id)
    step_data = steps.setdefault(step_key, {})
    step_data["output"] = signals
    self.validation_run.summary = summary
    self.validation_run.save(update_fields=["summary"])
```

The `_extract_downstream_signals()` method on `StepOrchestrator`
(`validibot/validations/services/step_orchestrator.py`) reads these
stored outputs back for downstream steps, structuring them as
`{step_key: {"output": {...}}}`.


## Path resolution

The `resolve_path()` function in
`validibot/validations/services/path_resolution.py` handles dotted and
bracket notation for navigating nested dict/list payloads. Both
`_build_cel_context()` and `resolve_workflow_signals()` use this shared
function.

Supported syntax:

- Dotted paths: `building.envelope.wall.u_value`
- Bracket notation: `results[0].temp`
- Mixed: `building.floors[0].zones[1].sensors[2]`


## Key function reference

| Function | File | Purpose |
|----------|------|---------|
| `resolve_workflow_signals()` | `services/signal_resolution.py` | Resolve WorkflowSignalMapping rows against submission data |
| `validate_signal_name()` | `services/signal_resolution.py` | Validate signal name is a valid CEL identifier and not reserved |
| `validate_signal_name_unique()` | `services/signal_resolution.py` | Cross-table uniqueness check (both models) |
| `_build_cel_context()` | `validators/base/base.py` | Build the namespaced CEL context for assertion evaluation |
| `_inject_promoted_outputs()` | `validators/base/base.py` | Promote output signal_name values into the s namespace |
| `_resolve_bound_input_context()` | `validators/base/base.py` | Resolve step-bound input signals from submission data |
| `_resolve_path()` | `validators/base/base.py` | Wrapper for shared path resolution |
| `resolve_path()` | `services/path_resolution.py` | Shared dotted/bracket path resolution |
| `store_signals()` | `services/step_processor/base.py` | Persist output signals to run summary |
| `_extract_downstream_signals()` | `services/step_orchestrator.py` | Collect outputs from prior steps for the steps namespace |
| `_resolve_workflow_signals()` | `services/step_orchestrator.py` | Orchestrator-level call to resolve_workflow_signals |
| `extract_output_signals()` | `validators/energyplus/validator.py` | Extract signals from EnergyPlus output envelope |
| `sync_fmu_catalog()` | `services/fmu.py` | Create FMU SignalDefinition rows from model introspection |
| `evaluate_assertions_for_stage()` | `validators/base/base.py` | Evaluate assertions against signal context |

## Related documentation

- [Signals Tutorial Example](signals-tutorial-example.md) -- End-to-end worked example
- [Validators](validators.md) -- Signal definition model and seed data
- [Assertions](assertions.md) -- How signals are referenced in rules
- [Step Processor](../overview/step_processor.md) -- Signal extraction and storage implementation
- [Workflow Engine](../overview/workflow_engine.md) -- Signal flow through workflow execution
- [Results](results.md) -- How signal values appear in run summaries
