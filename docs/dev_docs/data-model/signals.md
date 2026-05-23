# Signals

Signals (workflow vocabulary) and their step-local cousins (step inputs
and step outputs) are how named values flow through a validation run.
They let workflow authors write assertions that reference data by name
rather than by hard-coded paths.

This doc explains the mental model, the CEL context structure, the
underlying Django models, and the runtime flow. For a worked example, see
[Signals Tutorial Example](signals-tutorial-example.md). For the
user-facing CEL reference, see
[CEL Expressions](../../help_pages/concepts/cel-expressions.md).

## The mental model

Validibot organizes named values into five places in a CEL assertion's
context, distinguished by **scope** (workflow-wide vs. step-local) and by
**authorship** (who picks the name).

```
                Workflow vocabulary  (s.*)           module scope
                       ▲              ▲
                       │ promote      │ promote
                       │              │
            Step inputs (i.*)    Step outputs (o.*)  function scope
                   ▲                    ▲
                   │                    │
            parser facts          container output
            resolved bindings     derived signals
            template variables
```

`p.*` sits to the side as "the raw submission" — always present, not in
any scope hierarchy because it isn't named.

| Namespace | Scope | Who names it | Examples |
|---|---|---|---|
| `p.*` / `payload.*` | Raw submission, always available | (no naming — raw data) | `p.metadata.client_id` |
| `s.*` / `signal.*` | Workflow-wide vocabulary | Workflow author (signal mapping or promotion) | `s.target_eui` |
| `i.*` / `input.*` | Step-local — what the validator sees at the start | Validator catalog (parser facts) or upstream config (resolved bindings) | `i.zone_count`, `i.idf_version` |
| `o.*` / `output.*` | Step-local — what the validator produced after running | Validator catalog | `o.site_eui_kwh_m2` |
| `steps.<key>.input.*` / `steps.<key>.output.*` | Cross-step (downstream) | Same as `i.*` and `o.*`, just qualified by step | `steps.preflight.output.warning_count` |

The teaching analogy worth keeping in mind: **each step is a function**.
Inputs (`i.*`) are its parameters. Outputs (`o.*`) are what it returns.
The workflow vocabulary (`s.*`) is module-level state shared across
functions. Any function-local value can be promoted into module state via
"Copy to Signal" — works for inputs and outputs symmetrically.

## When do step inputs and step outputs exist?

A natural question once you've learned the namespaces is: *"Why are
`i.*` and `o.*` sometimes empty?"* The answer is precise enough to be a
test:

> **A step populates `i.*` or `o.*` only when it runs a process that
> transforms data.** If the validator just checks structural rules over
> the payload, both namespaces stay empty — the assertion author works
> entirely with `p.*` and `s.*`.

Validibot's validators occupy three positions on this spectrum.

### Position 1: no process — only `p.*` and `s.*` apply

Validators that check structural rules over the submitted payload
without transforming it. The payload IS the data; there's no derived
view to expose.

- **JSON Schema** — validates JSON against a JSON Schema document
- **XML Schema** — validates XML against an XSD
- **Basic** — applies CEL or comparison rules over the payload directly

For these, both `i.*` and `o.*` are empty. Results emerge as findings,
not as named values to assert against.

### Position 2: process produces outputs — only `o.*` populated

Validators that parse or evaluate a structured payload and emit results
as named values. No separate pre-execution input stage — the parser IS
the work.

- **SHACL** — parses RDF, runs shape constraints, emits violation counts
  and namespace flags
- **THERM** — parses THMX XML and emits 14 facts (polygon count, mesh
  level, BC temperatures, etc.)

For these, `i.*` is empty; `o.*` is the author's primary surface.

### Position 3: process has discrete input and output stages — both `i.*` and `o.*` populated

Validators that translate an arcane payload format into named facts
before doing their main work, then produce computed results after.

- **EnergyPlus** — parses IDF into facts (`i.zone_count`,
  `i.idf_version`), runs simulation, emits metrics
  (`o.site_eui_kwh_m2`, `o.unmet_hours`)
- **FMU** — resolves model input variables (`i.setpoint_temp`), runs
  simulation, emits results (`o.T_room`, `o.Q_cooling_actual`)

For these, both namespaces are meaningful at the appropriate stages.

### The bright-line test

"Does this validator have a process that transforms data?" is a yes/no
question with a clear answer per validator. That's what makes the
spectrum precise rather than fuzzy. The corresponding empty-state UX
messages in the step UI's Inputs/Outputs panels honestly tell authors
*why* each panel is or isn't populated for the validator they're using.

## Four concepts at the data layer

The model distinguishes four kinds of named values:

### 1. Workflow signals — `WorkflowSignalMapping` (the `s.*` namespace)

Author-defined values mapped to paths in the submission payload. Resolved
once before any step runs; visible to every step.

- Created by the workflow author via the "Edit Signals" UI on the workflow
  page
- Each mapping has a name (the CEL identifier) and a source path (a
  dotted/bracket path into the submission data)
- Available as `s.<name>` in every step

### 2. Step inputs — `StepIODefinition` with `direction=INPUT` (the `i.*` namespace)

Step-local values the validator has at the start of a step, before its
container or main work runs. Three sources feed `i.*`:

- **Parser-extracted facts** — values the validator extracts from the
  submission payload via its `extract_input_signals()` classmethod (e.g.
  EnergyPlus parses the IDF and exposes `i.zone_count`, `i.idf_version`).
  Source for arcane-format validators that ship a parser.
- **Resolved StepInputBindings** — values resolved from author-configured
  bindings before the container runs. FMU's model input variables are the
  canonical example: the .fmu file declares its inputs; the author binds
  each to a payload path or signal; the launcher resolves them and places
  the values in `i.*`. EnergyPlus template variables work the same way.
- **Catalog-declared inputs with no binding** — declared in the validator
  catalog but with `on_missing = "null"` so they default to null when not
  resolved. Rare in practice; appears mostly during catalog evolution.

`i.*` values are **step-local**. `i.zone_count` in one step has no
relationship to `i.zone_count` in another step (different submissions,
different parses). For workflow-wide access, promote the input to a
signal.

### 3. Step outputs — `StepIODefinition` with `direction=OUTPUT` (the `o.*` namespace)

Step-local values the validator produces after running. The catalog
declares the contract (slug, type, description); `extract_output_signals()`
populates the values from the container's output envelope.

`o.*` values are **temporally bound** — only available in output-stage
assertions on the producing step. An input-stage assertion that
references `o.*` resolves to null at runtime. Strict edit-time
rejection is partially implemented (the autocomplete supports a
``stage`` filter, and CEL classifier recognizes ``i.*`` references);
threading the stage parameter through every view call site to enforce
strict rejection at submit time is planned follow-up work tracked in
ADR-2026-05-22.

### 4. Promoted signals (the bridge between `i.*`/`o.*` and `s.*`)

Any step-local input/output definition — input or output — can be
promoted into the workflow vocabulary by setting
`StepIODefinition.promoted_signal_name` to a workflow-wide name. After
promotion:

- The original `i.<contract_key>` or `o.<contract_key>` still exists
  (step-local, validator-named)
- A new `s.<promoted_signal_name>` exists (workflow-wide, author-named)
- Both resolve to the same underlying value

Promotion is the *explicit ceremony* for "lift this from step-local to
workflow-wide." Authors trigger it via the "Copy to Signal" control on
the inputs or outputs table.

### Summary table

| Concept | CEL namespace | Model | Scope | Stage |
|---------|:-------------|:------|:------|:------|
| Workflow signals | `s.<name>` | `WorkflowSignalMapping` | All steps | Resolved before any step runs |
| Step inputs | `i.<contract_key>` | `StepIODefinition` (direction=INPUT) | Current step | Input stage onwards |
| Step outputs | `o.<contract_key>` | `StepIODefinition` (direction=OUTPUT) | Current step | Output stage only |
| Promoted signals | `s.<promoted_signal_name>` | `StepIODefinition` (with `promoted_signal_name`, either direction) | Downstream steps only | After producing step completes |
| Cross-step access | `steps.<step_key>.input.<name>` / `steps.<step_key>.output.<name>` | Run summary storage | Downstream steps | After producing step completes |
| Raw payload | `p.<path>` / `payload.<path>` | (none — raw data) | Current step | Always |

## The CEL context structure

Every CEL expression evaluates against a context with five namespaces and
their long-form aliases. The context is built by `_build_cel_context()`
in `validibot/validations/validators/base/base.py`.

```python
context = {
    "p": payload,            # alias for payload
    "payload": payload,      # raw submission or validator output data
    "s": signals_dict,       # alias for signal
    "signal": signals_dict,  # workflow signals + promoted values
    "i": inputs_dict,        # alias for input
    "input": inputs_dict,    # parser facts + resolved bindings (this step)
    "o": output_dict,        # alias for output
    "output": output_dict,   # this step's declared output signals
    "steps": steps_context,  # inputs and outputs from completed upstream steps
}
```

### `p` / `payload` — raw submission data

Always present. Contains the raw submission payload (for input-stage
assertions) or the validator's output envelope (for output-stage
assertions). Authors access raw fields via dotted notation:
`p.building.envelope.wall_r_value` or `payload.results[0].value`.

### `s` / `signal` — workflow vocabulary

Contains the merged workflow-wide signal namespace, built from two
sources:

1. **Workflow-level signals** from `RunContext.workflow_signals`
   (resolved from `WorkflowSignalMapping` rows before any step runs)
2. **Promoted values** from `StepIODefinition` rows with non-empty
   `promoted_signal_name` (injected by `_inject_promoted_outputs()`
   after the producing step completes — works for both input and output
   promotions; the legacy method name reflects the original
   output-only implementation)

Workflow signals take precedence over promoted values if there's a name
collision (workflow-defined names are the more stable identifier; the
collision suggests the author meant to refer to the workflow mapping).

Authors access signals via `s.target_eui` or `signal.target_eui`.

### `i` / `input` — step-local input values

Populated when this step begins, before its container runs (or before its
main in-process work for built-in validators). Three sources:

1. **Parser-extracted facts** from the validator's
   `extract_input_signals(payload)` classmethod. Validators that
   understand an arcane format implement this to expose useful facts about
   the submission before doing their main work. EnergyPlus extracts IDF
   facts; FMU could extract `modelDescription.xml` facts (currently
   unused).
2. **Resolved StepInputBinding values** for inputs declared with
   `direction=INPUT` and bound to a payload path or signal. The launcher
   resolves each binding against the submission data before invoking the
   container. These are also merged into the contract-keyed `i.*`
   namespace at input stage so input-stage assertions can reference them
   alongside parser-extracted facts.
3. **Catalog defaults** for declared inputs that have neither a parser
   value nor a resolved binding — typically null with `on_missing="null"`.

`i.*` is step-local. Different steps using the same validator on
different payloads get different `i.*` values; references don't cross
step boundaries.

Authors access input values via `i.zone_count` or `input.zone_count`.

### `o` / `output` — step output values

Populated after the validator runs. For output-stage assertions, this
contains the extracted output dict produced by `extract_output_signals()`.
For input-stage assertions, `o.*` is empty (or null-defaulted) — the
container hasn't run yet.

Authors access output values via `o.site_eui_kwh_m2` or
`output.site_eui_kwh_m2`. The autocomplete supports a stage filter that
can hide this step's `o.*` from input-stage editors; strict form-level
rejection of `o.*` references in input-stage assertions at submit time
is partially implemented and planned to land via a follow-up. Until
then, `o.*` references in input-stage assertions silently resolve to
null at runtime rather than being caught at edit time.

### `steps` — cross-step inputs and outputs

Contains both inputs and outputs from completed upstream steps. Each
entry is keyed by the step's `step_key` and contains `input` and `output`
sub-dicts:

```json
{
  "preflight": {
    "input": { "idf_version": "25.1", "zone_count": 12 },
    "output": { "warning_count": 3, "fatal_count": 0 }
  },
  "energyplus_step": {
    "input": { "idf_version": "25.1", "zone_count": 12 },
    "output": { "site_eui_kwh_m2": 75.2 }
  }
}
```

Authors access cross-step values via
`steps.preflight.output.warning_count` or
`steps.preflight.input.zone_count`.

### CEL expression examples

```cel
# Workflow signal (mapped from submission data)
s.target_eui < 100

# Promoted output from a prior step
s.simulated_eui < s.target_eui

# This step's input (parser-extracted IDF fact)
i.zone_count >= 4 && i.idf_version.startsWith("25.")

# This step's output (only in output-stage assertions)
o.site_eui_kwh_m2 < s.target_eui

# Compare input against output (cross-stage, in an output-stage assertion)
abs(i.expected_floor_area - o.floor_area_m2) < 5.0

# Raw payload access
p.building.envelope.wall_r_value > 10

# Cross-step output
steps.energyplus_step.output.site_eui_kwh_m2 < 100

# Cross-step input (e.g., reusing a parser fact from an earlier step)
steps.preflight.input.zone_count == steps.energyplus_step.input.zone_count

# Null guard for optional signals
s.max_unmet_hours != null && o.unmet_hours < s.max_unmet_hours
```

## Stage-aware assertion authoring

An assertion's stage (input vs. output) determines which namespaces are
available in CEL. The assertion form (`RulesetAssertionForm` in
`validibot/validations/forms.py`) enforces this at edit time.

| Editing an… | Available namespaces | Rejected at form-validation time |
|---|---|---|
| Input-stage assertion | `p.*`, `s.*`, `i.*`, `steps.<earlier>.input.*`, `steps.<earlier>.output.*` | `o.*` (this step's outputs don't exist yet) |
| Output-stage assertion | All of the above PLUS this step's `o.*` and `i.*` | (none) |

The autocomplete in the assertion-target widget is also filtered by
stage — the variable picker for an input-stage assertion does not offer
`o.*` entries, so authors aren't tempted by references that would silently
resolve to null.

The check is performed by `get_catalog_choices()` in
`validibot/workflows/mixins.py`, which takes a `stage` parameter and
returns the right subset.

## Model: `WorkflowSignalMapping`

**File**: `validibot/workflows/models.py`

Defines a workflow-level signal — an author's named vocabulary entry for
a data point in the submission payload. Each row maps a signal name to a
source path. Resolved once before any step runs; available to every step.

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
  guard produces a fail-fast evaluation error with guidance on how to fix
  it.

### Example

A workflow that validates energy models might define:

| name | source_path | on_missing |
|------|-------------|:----------:|
| `target_eui` | `metadata.target_eui_kwh_m2` | error |
| `building_type` | `metadata.building_type` | null |
| `floor_area` | `building.gross_floor_area_m2` | error |

All three signals become available as `s.target_eui`, `s.building_type`,
and `s.floor_area` in every step's CEL expressions.

## Model: `StepIODefinition`

**File**: `validibot/validations/models.py`

The stable data contract for a named step input or step output at the
validator or step level. A `StepIODefinition` declares that a validator
or workflow step expects (input, `i.*`) or produces (output, `o.*`) a
named data point with a specific type. It is the "what" — the contract —
not the "where" (that is the binding, `StepInputBinding`).

This model was previously named `SignalDefinition`. The rename landed
with [ADR-2026-05-22b](../../../../../validibot-project/docs/adr/2026-05-22-signals-vs-step-io-terminology.md);
the database table (`validations_signaldefinition`) was kept stable to
avoid a destructive rename across mature data.

This model unifies step input/output metadata that was previously
scattered across three legacy storage formats (`ValidatorCatalogEntry`,
FMU config JSON, template config JSON) into a single relational table.

### Key concepts

**`contract_key` vs `native_name`**: `contract_key` is the stable,
slug-safe identifier used in CEL expressions, the API, and data path
bindings (e.g., `panel_area`). `native_name` preserves the provider's
original name verbatim (e.g., an FMU's `Panel.Area_m2` or an EnergyPlus
template variable `#{heating_setpoint}`). The `contract_key` is what
Validibot uses; the `native_name` is what the provider uses.

**Ownership (XOR constraint)**: Each definition is owned by exactly one
of:

- A `Validator` — shared step input/output definitions that apply to
  every step using that validator (library validators).
- A `WorkflowStep` — per-step definitions for step-level FMU uploads,
  template scans, or author-customized inputs/outputs.

This is enforced by the `ck_sigdef_one_owner` database constraint.

**Promotion via `promoted_signal_name`**: When a `StepIODefinition` has
a non-empty `promoted_signal_name`, its resolved value is promoted into
the `s.*` (workflow vocabulary) namespace, available in all downstream
steps. This works for **both directions**:

- An OUTPUT-direction definition with
  `promoted_signal_name="simulated_eui"` makes its value available as
  `s.simulated_eui` after the producing step runs.
- An INPUT-direction definition with
  `promoted_signal_name="zone_count"` makes its parsed/resolved value
  available as `s.zone_count` from the producing step's input-stage
  processing onwards — **but only in downstream steps**, never within
  the producing step itself (the temporal rule from
  ADR-2026-05-22b).

This symmetric promotion is the bridge between step-local namespaces
(`i.*`, `o.*`) and the workflow vocabulary (`s.*`).

### Fields

| Field | Type | Purpose |
|-------|------|---------|
| `contract_key` | `SlugField(255)` | Stable slug identifier used in CEL, API, and bindings. |
| `native_name` | `CharField(500)` | Provider's original name, preserved verbatim. |
| `label` | `CharField(255)` | Human-readable display label. |
| `description` | `TextField` | Detailed description. |
| `direction` | `CharField(10)` | `INPUT` (→ `i.*`) or `OUTPUT` (→ `o.*`), from `SignalDirection` choices. |
| `data_type` | `CharField(20)` | Value type: `NUMBER`, `STRING`, `BOOLEAN`, `TIMESERIES`, `OBJECT`. |
| `origin_kind` | `CharField(20)` | How created: from config declaration, FMU probe, or template scan. |
| `source_kind` | `CharField(20)` | How the value is obtained: `PAYLOAD_PATH` or `INTERNAL` (see below). |
| `on_missing` | `CharField(10)` | Behavior when value can't be resolved: `error`, `null`, or `ignore`. Default `null`. |
| `is_path_editable` | `BooleanField` | Whether the workflow author can edit the source data path in the step binding. |
| `validator` | FK to `Validator` (nullable) | Owner for library validators. XOR with `workflow_step`. |
| `workflow_step` | FK to `WorkflowStep` (nullable) | Owner for step-level signals. XOR with `validator`. |
| `order` | `PositiveIntegerField` | Display ordering within the owner's signal list. |
| `is_hidden` | `BooleanField` | Hidden from the default signals UI. |
| `unit` | `CharField(50)` | Unit of measurement (e.g., `kW`, `m2`, `degC`). |
| `provider_binding` | `JSONField` | Validator-type-specific binding properties (see below). |
| `metadata` | `JSONField` | Arbitrary metadata for extensions and integrations. |
| `promoted_signal_name` | `CharField(100)` | Promotion name (in-row, applies to step-owned rows). When set, value is available as `s.<promoted_signal_name>` in downstream steps. Works for any direction. The Python field was renamed in migration 0051; the database column was renamed along with it. Validator-owned rows (shared catalog entries) carry workflow-scoped promoted names via the separate `WorkflowStepIOPromotion` overlay table — see the "Two promotion sources" section below. |

### Constraints

| Constraint | Fields | Purpose |
|------------|--------|---------|
| `ck_sigdef_one_owner` | `validator`, `workflow_step` | Exactly one owner (XOR). |
| `uq_sigdef_validator_key_dir` | `validator`, `contract_key`, `direction` | Unique per validator. |
| `uq_sigdef_step_key_dir` | `workflow_step`, `contract_key`, `direction` | Unique per step. |

### Two promotion sources: in-row vs. overlay

`StepIODefinition` rows have two ownership patterns, and promotion
storage differs accordingly:

**Step-owned rows** (`workflow_step` FK set, `validator` null) — the
in-row `promoted_signal_name` field holds the workflow-scoped
promotion name. One owner means no scope ambiguity.

**Validator-owned rows** (`validator` FK set, `workflow_step` null —
e.g. the EnergyPlus catalog entries) — these rows are shared across
every workflow that uses the validator, so the in-row field can't
carry a workflow-scoped name without colliding across workflows. The
promotion lives in a separate `WorkflowStepIOPromotion` overlay table
keyed on `(workflow_step, signal_definition)` so each workflow gets
its own promoted name pointing at the same shared catalog row.

The runtime injection in `_inject_promoted_outputs()`, the autocomplete
in `get_catalog_choices()`, the Step Inputs/Outputs tables, the
Available Data panel, and the workflow versioning clone all consult
**both** sources — read paths merge them so the overlay is a
first-class part of the workflow contract, not a secondary cache.

The overlay model was introduced by the May 2026 P1 fix; before
then, Copy-to-Signal on validator-owned catalog rows would 404
because the promote view required a step-owned row.

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
distinction is surfaced in the UI so workflow authors know which signals
they can configure and which are fixed by the validator.

**`PAYLOAD_PATH`** (default): The signal's value comes from a known data
path in the submission payload. The workflow author may (depending on
`is_path_editable`) configure the exact path via the step's signal
binding. Most FMU input signals and template signals use this mode — the
author wires each input to the right field in their submission data.

**`INTERNAL`**: The validator has its own mechanism for extracting or
computing the value. Examples include EnergyPlus parser-extracted facts
(via `extract_input_signals()`), EnergyPlus simulation metrics (via
`extract_output_signals()`), THERM signals (parsed inline), and FMU
output variables (read from the FMU runtime). The source path in the
step binding is typically fixed and should not be changed by the author.

**`is_path_editable`** controls whether the source data path field in the
signal edit modal is enabled or disabled. When `False`, Django's
`field.disabled = True` provides server-side protection — even if someone
tampers with the form HTML, Django ignores the submitted value.

| Validator | Direction | `source_kind` | `is_path_editable` |
|-----------|-----------|:-------------|:-------------------|
| EnergyPlus | Input (parser facts) | `INTERNAL` | `False` |
| EnergyPlus | Input (template variables) | `PAYLOAD_PATH` | `True` |
| EnergyPlus | Output | `INTERNAL` | `False` |
| THERM | Output | `INTERNAL` | `False` |
| FMU | Input (model variables) | `PAYLOAD_PATH` | `True` |
| FMU | Output | `INTERNAL` | `False` |
| Custom | Any | `PAYLOAD_PATH` | `True` |

### `on_missing` behavior on catalog signals

The same three-mode semantics as `WorkflowSignalMapping.on_missing`, but
applied per catalog row:

- **`error`** — value must be resolvable; run fails with a clear message
  if not. Use for signals that downstream assertions reliably depend on
  (e.g. `idf_version` is required because every IDF has a Version
  object).
- **`null`** (default) — inject null when value can't be resolved.
  Assertions must guard with `has(...)` or `!= null`. Surface in the
  library page as "may be null."
- **`ignore`** — omit silently from the context. References resolve to
  null but don't surface as anything special. Use for genuinely optional
  facts the author shouldn't need to know about.

### Typed metadata accessors

`StepIODefinition` provides typed access to provider-specific metadata
through Pydantic accessor properties:

- `sig.fmu_binding` — `FMUProviderBinding` (causality, value_reference, etc.)
- `sig.fmu_metadata` — `FMUSignalMetadata` (display hints)
- `sig.template_metadata` — `TemplateSignalMetadata` (variable type, constraints)

## How the two models relate

`WorkflowSignalMapping` and `StepIODefinition` serve different roles,
but they all interact through the same CEL context.

```
WorkflowSignalMapping                 StepIODefinition (INPUT)
(workflow-level)                      (validator/step-level)

name: "target_eui"                    contract_key: "zone_count"
source_path: "metadata.target_eui"    direction: INPUT
                                      promoted_signal_name: ""
        │                                      │
        ▼                                      ▼
   s.target_eui                          i.zone_count
        │                                      │
        └──────────── CEL ─────────────────────┘
                      │
        i.zone_count >= 4 && s.target_eui < 100


StepIODefinition (INPUT, promoted)    StepIODefinition (OUTPUT, promoted)

contract_key: "zone_count"            contract_key: "site_eui_kwh_m2"
direction: INPUT                      direction: OUTPUT
promoted_signal_name: "zone_count"    promoted_signal_name: "simulated_eui"
        │                                      │
        ▼ promote                              ▼ promote
   i.zone_count                          o.site_eui_kwh_m2
        │                                      │
        └─►  s.zone_count    s.simulated_eui  ◄┘
              (workflow-wide, available downstream)
```

**WorkflowSignalMapping** creates signals by extracting values from
submission data. Resolved once before any step runs.

**StepIODefinition** declares the inputs and outputs of individual
validators and steps. Either direction can be promoted to the workflow
vocabulary by setting `promoted_signal_name`.

## Cross-table signal name uniqueness

Signal names must be unique within a workflow across both models. A
workflow cannot have a `WorkflowSignalMapping` named `floor_area` and a
promoted `StepIODefinition` with `promoted_signal_name="floor_area"` in
the same workflow.

This is enforced at the application level by
`validate_signal_name_unique()` in
`validibot/validations/services/signal_resolution.py`. The function
queries both tables:

1. Checks `WorkflowSignalMapping.objects.filter(workflow_id=..., name=...)`
2. Checks `StepIODefinition.objects.filter(workflow_step__workflow_id=..., promoted_signal_name=...)` —
   any direction; with symmetric input promotion, an INPUT-direction
   `promoted_signal_name` collides with the same vigour as an
   OUTPUT-direction one.

Both models call this function in their `clean()` methods.

Additionally, `validate_signal_name()` checks that names are valid CEL
identifiers and not reserved words. The reserved names list includes all
CEL context keys (`p`, `payload`, `s`, `signal`, `i`, `input`, `o`,
`output`, `steps`), CEL built-in functions, and CEL keywords.

## Signals vs custom data paths

Assertions in Validibot target data in one of two ways.

### Declared signals (the data contract)

When a validator author defines signals, they are publishing a **data
contract**: "this validator knows about these specific data points."
Signals have names (slugs), types, stages (input or output), and
metadata. They appear in dropdowns, support type-appropriate operators,
and enable compile-time validation of CEL expressions.

This is the structured, guided path. The validator author has done the
work of mapping data paths (or parser extraction) to meaningful names,
and workflow authors benefit from that investment.

Examples of validators with declared signals:

- **EnergyPlus** declares output signals for simulation metrics plus
  input signals for parser-extracted IDF facts
- **FMU** auto-discovers signals by introspecting the model's variables
- **Custom validators** where the author manually adds signals through the UI

### Custom data paths (no contract)

Some validators don't declare signals. The Basic validator, JSON Schema
validator, and XML Schema validator validate structure but don't
pre-declare what specific fields exist in the data. When a workflow
author uses one of these validators and wants to write assertions, they
reference data using **custom data paths** — dot-notation expressions
accessed via the `p` (payload) namespace, like
`p.building.thermostat.setpoint` or `p.results[0].value`.

This is the flexible, exploratory path. The workflow author navigates the
data shape themselves, without the guardrails that declared signals
provide.

### How the two modes interact

The `allow_custom_assertion_targets` flag on `Validator` controls whether
workflow authors can go beyond declared signals:

| Scenario | Signals exist? | Custom paths allowed? | What the author sees |
|----------|:-:|:-:|------|
| EnergyPlus | Yes (inputs + outputs) | No | Signal dropdown only |
| Custom validator with signals | Yes | Configurable | Dropdown + optional free-form paths |
| Basic validator | No | Yes (always) | Free-form path entry only |
| JSON Schema / XML Schema | No | Yes | Free-form path entry only |

When both modes are available, the form shows "Target Signal or Path" and
attempts to match user input against signal definitions first, falling
back to treating it as a custom path.

## Workflow-level signal resolution: `resolve_workflow_signals()`

**File**: `validibot/validations/services/signal_resolution.py`

This is the pre-step resolution phase. Before any workflow step executes,
all `WorkflowSignalMapping` rows are resolved against the submission
payload. The result is stored in `RunContext.workflow_signals` and
injected into the CEL context as the `s` / `signal` namespace.

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
`resolve_workflow_signals()` before each step execution. The resolved
dict is passed via `RunContext.workflow_signals` to the validator, which
injects it into the CEL context.

## Step-level input resolution: `extract_input_signals()` and bindings

**File**: `validibot/validations/validators/base/advanced.py` (the hook)

Before a step's container runs (or before in-process work for built-in
validators), the engine populates `i.*` from up to three sources:

### Parser-extracted facts

A validator that understands an arcane format implements
`extract_input_signals(payload)` to expose useful facts about the
submission. Signature:

```python
@classmethod
def extract_input_signals(cls, payload: Any) -> dict[str, Any] | None:
    """Extract input-stage facts from the submission.

    Returns a dict keyed by catalog contract_key, or None if not
    applicable. Called after preprocess_submission() so template-mode
    submissions are parsed against the resolved IDF.
    """
```

For EnergyPlus, this parses the IDF text and returns
`{"idf_version": "25.1", "zone_count": 12, "north_axis_deg": 0.0}`.

The base class returns `None`; validators opt in by overriding.

### Resolved StepInputBindings

For each `StepIODefinition` with `direction=INPUT` that has a
corresponding `StepInputBinding` row, the launcher resolves the
binding's `source_data_path` against the submission data. The resolved
value lands in `i.<contract_key>`.

For FMU steps, this is how the per-submission model input variables get
into `i.*`. For EnergyPlus template steps, this is how the template
variable values get into `i.*`.

### Catalog defaults

For declared inputs without parser values or resolved bindings, the
catalog's `on_missing` policy applies:

- `error` → run fails with a clear message before the container starts
- `null` → injected as `None`
- `ignore` → omitted from the dict (references resolve to null)

### Persistence

Resolved `i.*` values are persisted to the run summary under
`run.summary["steps"][step_key]["input"]` so they're available to
downstream steps via `steps.<key>.input.*`.

## Promoted signals reconstruction: `_inject_promoted_outputs()`

**File**: `validibot/validations/validators/base/base.py`

When a `StepIODefinition` (any direction) has a non-empty
`promoted_signal_name`, the resolved value is "promoted" into the `s.*`
namespace for downstream steps. The method name still reads
`_inject_promoted_outputs` for historical reasons (it originally only
handled outputs) — it now handles inputs and outputs uniformly.

### How it works

1. `_inject_promoted_outputs()` runs inside `_build_cel_context()` when
   the `steps` context is non-empty (i.e., there are completed upstream
   steps).
2. It queries `StepIODefinition` rows with non-empty
   `promoted_signal_name` across all **upstream** steps in the current
   workflow (filtered by `workflow_step__order__lt=current_step.order`
   to enforce the temporal rule — a step cannot see its own promotion).
3. For each promoted definition, it looks up the producing step's
   `step_key` in the `steps` context.
4. It extracts the value using the definition's `contract_key`:
   - For OUTPUT-direction promotions: from `step["output"][contract_key]`
   - For INPUT-direction promotions: from `step["input"][contract_key]`
5. If found, it injects the value into `signals_dict` under the
   `promoted_signal_name`.

### Why it runs on every step

Promoted values are only available after the producing step completes.
Since different steps may complete at different times (especially with
async validators), `_inject_promoted_outputs()` runs fresh on every
step rather than once at the start of the run.

### Example

Given a `StepIODefinition` for input promotion:

- `contract_key = "zone_count"`, `direction = INPUT`,
  `promoted_signal_name = "zone_count"`, on step with `step_key = "preflight"`

And a run summary:

```json
{"steps": {"preflight": {"input": {"zone_count": 12}}}}
```

The promotion injects `signals_dict["zone_count"] = 12`, making it
accessible as `s.zone_count` in downstream CEL expressions.

The same mechanism works for OUTPUT-direction promotions reading from
`step["output"][contract_key]`.

## How signals are defined

### Config-based definition (advanced validators)

Advanced validators define their step inputs/outputs in `config.py`
modules co-located with the validator code. Each config module exports
a `ValidatorConfig` instance containing a list of `CatalogEntrySpec`
objects that seed `StepIODefinition` rows. Each `CatalogEntrySpec` can
declare `source_kind`, `is_path_editable`, and `on_missing` to control
how the value is obtained and what happens when it can't be resolved.

**Key files**:

- `validibot/validations/validators/base/config.py` — `CatalogEntrySpec`
  and `ValidatorConfig` Pydantic models
- `validibot/validations/validators/energyplus/config.py` — EnergyPlus
  signal definitions
- `validibot/validations/validators/fmu/config.py` — FMU config (empty
  `catalog_entries`; signals created dynamically via introspection)

### Dynamic definition (FMU validators)

FMU validators don't predefine signals in config. Instead, when an FMU
file is uploaded, `sync_fmu_catalog()` in
`validibot/validations/services/fmu.py` introspects the FMU's
`modelDescription.xml`, discovers all input/output variables, and creates
`StepIODefinition` rows dynamically.

Each FMU variable's `causality` (input, output, parameter) determines
whether it becomes an INPUT or OUTPUT signal. The `contract_key` is
derived from the variable name via `slugify()`, and the `native_name`
preserves the original FMU variable name.

### Custom validators

Users can add signals to custom validators through the UI. The signal
definition forms handle creation and editing.

### Syncing configs to the database

The `sync_validators` management command
(`validibot/validations/management/commands/sync_validators.py`)
discovers all `ValidatorConfig` instances via `discover_configs()` and
upserts `Validator` + `StepIODefinition` rows.

```bash
python manage.py sync_validators
```

## How signals flow during execution

### Complete lifecycle

```
1. DEFINITION                     config.py, FMU introspection, or UI
   ├─ StepIODefinition            Django model rows (validator or step owned)
   └─ WorkflowSignalMapping       Django model rows (workflow-level)

2. WORKFLOW RUN STARTS            StepOrchestrator.execute_workflow_steps()
   ├─ resolve_workflow_signals()  Resolve WorkflowSignalMapping → s namespace
   └─ _extract_downstream_signals() Collect prior step inputs/outputs → steps namespace

3. EACH STEP EXECUTES             validate() + post_execute_validate()

   3a. INPUT STAGE
       ├─ preprocess_submission() Template-mode IDF substitution
       ├─ extract_input_signals() Parse facts from (resolved) payload → i namespace
       ├─ Resolve StepInputBindings    → i namespace
       ├─ store input dict to run summary
       ├─ _build_cel_context(stage="input")
       │     p, s, i, steps namespaces populated; o is empty
       ├─ _inject_promoted_outputs()   Promotions visible from completed upstreams
       └─ Evaluate input-stage assertions

   3b. EXECUTION
       └─ Container or in-process work runs

   3c. OUTPUT STAGE
       ├─ extract_output_signals()  → o namespace
       ├─ store output dict to run summary
       ├─ _build_cel_context(stage="output")
       │     all namespaces populated
       ├─ _inject_promoted_outputs()
       └─ Evaluate output-stage assertions

4. RUN SUMMARY STORAGE
   ├─ run.summary["steps"][step_key]["input"] = i dict
   └─ run.summary["steps"][step_key]["output"] = o dict
     Both available downstream as steps.<step_key>.input.* / .output.*
```

### Within a single step

1. **Preprocessing.** For EnergyPlus template mode,
   `preprocess_submission()` substitutes template variables into the IDF
   so the submission looks like a direct-IDF upload by the time the
   parser runs. For other validators, this is a no-op.

2. **Input population.** `extract_input_signals()` parses the payload (if
   the validator implements it). Resolved StepInputBindings are
   collected. The merged dict becomes `i.*`.

3. **Input persistence.** The `i.*` dict is stored to the run summary
   under `run.summary["steps"][step_key]["input"]` so downstream steps
   can reach it.

4. **CEL context building (input stage).** `_build_cel_context()`
   assembles the namespaces: `p`/`payload` (raw data), `s`/`signal`
   (workflow signals + promoted values), `i`/`input` (this step's inputs),
   `o`/`output` (empty at input stage), and `steps` (upstream step inputs
   and outputs).

5. **Input-stage assertion evaluation.** CEL expressions reference
   signals via `s.*` and step-local inputs via `i.*`. The assertion form
   has already ensured no expression references `o.*` at this stage.

6. **Validator execution.** The validator runs (container launch,
   in-process check, AI call, etc.).

7. **Output extraction.** The validator returns extracted outputs. For
   advanced validators, `extract_output_signals()` converts the container
   output envelope into a flat dict.

8. **Output-stage assertion evaluation.** CEL expressions reference
   output values via `o.*` and may freely reference any other namespace.

9. **Output persistence.** The `o.*` dict is stored to the run summary
   under `run.summary["steps"][step_key]["output"]`.

### Across steps (cross-step communication)

Inputs and outputs from earlier steps are available to later steps in the
same run:

1. **Storage.** When step N completes, both its inputs and outputs are
   saved to `validation_run.summary`:

   ```json
   {
     "steps": {
       "preflight": {
         "input": {"idf_version": "25.1", "zone_count": 12},
         "output": {"warning_count": 3, "fatal_count": 0}
       },
       "energyplus_step": {
         "input": {"idf_version": "25.1", "zone_count": 12, "north_axis_deg": 0.0},
         "output": {"site_eui_kwh_m2": 87.5, "site_electricity_kwh": 12500}
       }
     }
   }
   ```

2. **Collection.** Before step N+1 runs,
   `StepOrchestrator._extract_downstream_signals()` reads the summary and
   collects inputs and outputs from all prior steps.

3. **Context injection.** The collected data is passed to the validator
   via `RunContext.downstream_signals`, then exposed in the CEL context
   under the `steps` namespace:

   ```cel
   steps.energyplus_step.output.site_eui_kwh_m2
   steps.preflight.input.zone_count
   ```

4. **Promoted values.** If any upstream step has a `StepIODefinition`
   with `promoted_signal_name`, `_inject_promoted_outputs()` places the
   value into the `s.*` namespace:

   ```cel
   s.simulated_eui < s.target_eui     # if upstream output promoted
   s.zone_count >= 4                  # if upstream input promoted
   ```

This lets a downstream step write assertions that reference upstream data
either by the full path (`steps.<key>.input.*` or `steps.<key>.output.*`)
or by promoted signal name (`s.<signal_name>`).

## CEL context building in detail

The `_build_cel_context()` method on `BaseValidator`
(`validibot/validations/validators/base/base.py`) is the heart of the
context assembly. It builds the dictionary that CEL expressions evaluate
against.

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

1. **Builds the `s` (signals) namespace** from two sources:
   - Workflow-level signals from `RunContext.workflow_signals`
   - Promoted values from upstream steps via `_inject_promoted_outputs()`

2. **Builds the `i` (inputs) namespace** from three sources:
   - Parser-extracted facts via the validator's
     `extract_input_signals()` (if implemented)
   - Resolved StepInputBinding values
   - Catalog defaults for declared inputs without resolved values

3. **Builds the `o` (outputs) namespace.** At the output stage, the full
   validator output payload is used. At the input stage, `o.*` is empty
   (or null-defaulted) — the container hasn't run.

4. **Builds the `steps` namespace** from `RunContext.downstream_signals`
   or the run summary, including both `input` and `output` sub-dicts per
   completed step.

5. **Assembles the final context** with all namespace keys: `p`,
   `payload`, `s`, `signal`, `i`, `input`, `o`, `output`, `steps`. All
   roots are always present (even if empty) so CEL expressions can
   reference them without undefined-variable errors.

## Output signal elevation pipeline

Output signals from advanced validators (FMU, EnergyPlus) go through a
multi-stage pipeline before they become CEL variables.

### Stage 1: Extraction — `extract_output_signals()`

Each advanced validator class defines an `extract_output_signals()`
classmethod that converts the container's output envelope into a flat
Python dict of signal names to values. For FMU validators, this dict
contains the final time-step values of each output variable:

```python
# FMU extract_output_signals() returns:
{"T_room": 296.63, "Q_cooling_actual": 5172.83}
```

This method is called in `AdvancedValidator.post_execute_validate()`
after the container completes. The extracted dict is stored in
`ValidationResult.signals` and later persisted to `run.summary` by the
processor's `store_signals()` method.

### Stage 2: Payload merging

Before output-stage assertions are evaluated, the validator output is
placed in the `o` / `output` namespace.

### Stage 3: CEL context building

`_build_cel_context(stage="output")` places the output dict in `o.*`,
keeps `i.*` populated from input-stage resolution, refreshes `s.*` with
any newly-promoted signals, and exposes upstream data via `steps.*`.

### Stage 4: CEL evaluation

When cel-python compiles the expression `o.T_room < 300.15`, it parses
the dot as **member access** — the standard CEL operator for selecting a
field from a map. At evaluation time:

1. CEL looks up the variable `o` in the activation context
2. Finds the Python dict `{"T_room": 296.63, ...}`
3. cel-python's `json_to_cel()` converts the dict to a CEL `MapType`
4. The `.T_room` selector retrieves the value `296.63` from the map
5. The comparison `296.63 < 300.15` evaluates to `true`

Standard CEL — no custom operators, no dialect extensions.

## Signal extraction for advanced validators

### EnergyPlus input extraction (parser facts)

**File**: `validibot/validations/validators/energyplus/validator.py`

```python
@classmethod
def extract_input_signals(cls, payload: Any) -> dict[str, Any] | None:
    """Parse the (resolved) IDF text and extract declared input facts.

    Returns a dict like {"idf_version": "25.1", "zone_count": 12, ...}
    keyed by catalog contract_key.
    """
```

Runs after `preprocess_submission()` so template-mode submissions are
parsed against the resolved IDF, not the unresolved JSON variable dict.

### EnergyPlus output extraction (simulation metrics)

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
defines all possible output fields. `model_dump()` converts them to a
dict, and `None` values are filtered out.

Not every signal will be populated for every IDF. The signal definitions
declare the full set of metrics that the validator knows how to extract,
but EnergyPlus only produces a value when the IDF is configured to
generate it. When a signal is absent from the extracted dict, the display
layer reports "Value not found" and the `on_missing` policy on the
catalog row determines runtime behaviour.

### FMU input resolution

**File**: `validibot/validations/services/cloud_run/launcher.py`

Before launching an FMU container, the launcher resolves input signals
from the submission payload using `StepIODefinition` rows with
`direction=INPUT`. Resolved values land in `i.*` for input-stage
assertions to reference.

## Storage

Signals are not stored in a dedicated table. They live in the `summary`
JSONField on `ValidationRun`, nested under
`steps.<step_key>.input` and `steps.<step_key>.output`. This keeps signal
storage lightweight (no extra rows per signal per run) and naturally
scoped to the run lifecycle.

The `store_signals()` method on `ValidationStepProcessor`
(`validibot/validations/services/step_processor/base.py`) handles
persistence:

```python
def store_signals(
    self,
    signals: dict[str, Any],
    *,
    stage: str,
) -> None:
    if not signals:
        return
    summary = self.validation_run.summary or {}
    steps = summary.setdefault("steps", {})
    step_key = self.step_run.workflow_step.step_key or str(self.step_run.id)
    step_data = steps.setdefault(step_key, {})
    step_data[stage] = signals  # stage is "input" or "output"
    self.validation_run.summary = summary
    self.validation_run.save(update_fields=["summary"])
```

The `_extract_downstream_signals()` method on `StepOrchestrator`
(`validibot/validations/services/step_orchestrator.py`) reads these
stored values back for downstream steps, structuring them as
`{step_key: {"input": {...}, "output": {...}}}`.

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
| `resolve_workflow_signals()` | `services/signal_resolution.py` | Resolve `WorkflowSignalMapping` rows against submission data |
| `validate_signal_name()` | `services/signal_resolution.py` | Validate signal name is a valid CEL identifier and not reserved |
| `validate_signal_name_unique()` | `services/signal_resolution.py` | Cross-table uniqueness check (both models, any direction) |
| `_build_cel_context()` | `validators/base/base.py` | Build the namespaced CEL context for assertion evaluation |
| `_inject_promoted_outputs()` | `validators/base/base.py` | Inject promoted input and output values into the `s` namespace (method retains legacy name) |
| `_resolve_bound_input_context()` | `validators/base/base.py` | Resolve step-bound input signals from submission data |
| `_resolve_path()` | `validators/base/base.py` | Wrapper for shared path resolution |
| `resolve_path()` | `services/path_resolution.py` | Shared dotted/bracket path resolution |
| `store_signals()` | `services/step_processor/base.py` | Persist input/output signals to run summary |
| `_extract_downstream_signals()` | `services/step_orchestrator.py` | Collect inputs and outputs from prior steps for the `steps` namespace |
| `_resolve_workflow_signals()` | `services/step_orchestrator.py` | Orchestrator-level call to `resolve_workflow_signals` |
| `extract_input_signals()` | `validators/base/advanced.py` (base) | Parse input-stage facts from the submission; overridden per validator |
| `extract_output_signals()` | `validators/energyplus/validator.py` etc. | Extract signals from a validator's output envelope |
| `sync_fmu_catalog()` | `services/fmu.py` | Create FMU `StepIODefinition` rows from model introspection |
| `evaluate_assertions_for_stage()` | `validators/base/base.py` | Evaluate assertions against signal context |
| `get_catalog_choices()` | `workflows/mixins.py` | Build the stage-aware variable autocomplete for the assertion form |

## Related documentation

- [Signals Tutorial Example](signals-tutorial-example.md) — End-to-end worked example
- [Validators](validators.md) — Catalog model and seed data
- [Assertions](assertions.md) — How signals, step inputs, and step outputs are referenced in rules
- [Step Processor](../overview/step_processor.md) — Step input/output extraction and storage implementation
- [Workflow Engine](../overview/workflow_engine.md) — Value flow through workflow execution
- [Results](results.md) — How values appear in run summaries
- [CEL Expressions (user-facing)](../../help_pages/concepts/cel-expressions.md) — Author-oriented namespace reference
- [ADR-2026-05-22](../../../../../validibot-project/docs/adr/2026-05-22-energyplus-catalog-cleanup-and-parsed-outputs.md) — EnergyPlus catalog cleanup and the `i.*` namespace
- [ADR-2026-05-22b](../../../../../validibot-project/docs/adr/2026-05-22-signals-vs-step-io-terminology.md) — Terminology (signal vs. step input/output) and model rename

---

## Appendix: code-vs-vocabulary

The vocabulary used throughout this doc — *signal*, *step input*, *step
output*, *promotion* — matches the user-facing UI and the public
documentation. The Python class identifiers were aligned with this
vocabulary in May 2026 per
[ADR-2026-05-22b](../../../../../validibot-project/docs/adr/2026-05-22-signals-vs-step-io-terminology.md).
The underlying database table names and a handful of URL slugs were
intentionally left alone to avoid a destructive rename on mature data
and to avoid churning every workflow link in the codebase.

| Concept (this doc) | Python class / field | Database table / column |
|---|---|---|
| Step IO definition (one row per step input or step output) | `StepIODefinition` | `validations_signaldefinition` (legacy name retained) |
| Step input binding (binds a step input to a payload path or signal) | `StepInputBinding` | `validations_stepsignalbinding` (legacy name retained) |
| In-row promotion field (step-owned rows) | `promoted_signal_name` | `signal_name` column on the step IO table |
| Overlay promotion for validator-owned rows | `WorkflowStepIOPromotion(workflow_step, signal_definition, promoted_signal_name)` | `validations_workflowstepiopromotion` |
| Workflow-level signal definition | `WorkflowSignalMapping` | `validations_workflowsignalmapping` |

Method names like `_inject_promoted_outputs` (which now handles both
in-row and overlay promotions, for inputs and outputs) retain their
legacy "output"-flavored naming because renaming them for cosmetic
parity would churn every caller for no behavioural gain.
