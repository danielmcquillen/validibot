# Signals, Step Inputs, and Step Outputs

Validibot organizes the data you can assert against into three distinct
concepts: **signals**, **step inputs**, and **step outputs**.
Understanding the difference is key to writing clear workflows and
assertions.

This page is the user-friendly tour. For the underlying architecture,
see [Signals (developer reference)](../../dev_docs/data-model/signals.md);
for the precise vocabulary decision, see
[ADR-2026-05-22b](../../../../../validibot-project/docs/adr/2026-05-22-signals-vs-step-io-terminology.md).

---

## The mental model in one paragraph

Think of each workflow step as a function in a program. A step can have
**step inputs** (`i.<name>`) — values the validator has at the start of
its work, like function parameters — and **step outputs** (`o.<name>`)
— values the validator produces, like a function's return value. The
workflow as a whole has a shared **signal vocabulary** (`s.<name>`) —
like module-level variables visible everywhere. You **promote** any
step-local value to a signal when you want it visible across steps.

---

## Signals

A **signal** is a named value in the workflow's vocabulary, accessible
to every step as `s.<name>`. You create signals two ways:

**Signal mapping** — on the workflow-level page, you define a signal by
giving it a name and a data path. This maps a raw submission value to a
clean, reusable name. For example, you might map the signal `floor_area`
to the data path `building_metadata.geometry.total_floor_area_m2`.

**Promotion** — you lift a step-local value (a step input or a step
output) into the signal vocabulary by clicking "Copy to Signal" and
choosing a workflow-wide name. If a step's parser extracts
`i.zone_count = 12`, you can promote it to a signal called `zone_count`
so that every other step can reference `s.zone_count`.

In CEL expressions you reference signals as `s.name` (or the long form
`signal.name`).

### Why signals matter — a before-and-after example

Consider a realistic building energy submission. The data arrives as a
deeply nested JSON payload because it follows an industry schema:

```json
{
  "project": {
    "id": "PRJ-2024-0142",
    "building": {
      "geometry": {
        "gross_floor_area_m2": 4800.0,
        "conditioned_floor_area_m2": 4200.0,
        "num_stories_above_grade": 3
      }
    },
    "compliance": {
      "targets": {
        "max_site_eui_kwh_m2": 120.0,
        "max_unmet_hours": 300,
        "min_renewable_fraction": 0.15
      }
    }
  }
}
```

**Without signals**, your CEL assertions reference the raw paths
directly:

```
p.project.building.geometry.gross_floor_area_m2 > 0
o.site_eui_kwh_m2 < p.project.compliance.targets.max_site_eui_kwh_m2
o.unmet_hours < p.project.compliance.targets.max_unmet_hours
```

These work, but they're fragile. If the submitter's schema changes
`compliance.targets` to `compliance.performance_targets`, every
assertion that references it breaks. They're also hard to read — the
business intent ("is the EUI within the target?") is buried under path
navigation.

**With signals**, you define the mapping once and write clean
assertions:

| Signal name | Data path |
|------------|-----------|
| `floor_area` | `project.building.geometry.gross_floor_area_m2` |
| `target_eui` | `project.compliance.targets.max_site_eui_kwh_m2` |
| `max_unmet_hours` | `project.compliance.targets.max_unmet_hours` |

Now your assertions become:

```
s.floor_area > 0
o.site_eui_kwh_m2 < s.target_eui
o.unmet_hours < s.max_unmet_hours
```

The business logic is immediately clear. If the data structure changes,
you update the data path in the signal mapping — the assertions stay
exactly the same. If you add a second workflow that validates the same
data with different rules, it can reuse the same signal names.

---

## Step inputs

A **step input** is a step-local value the validator can see at the
start of its work, before its main process runs. You reference step
inputs in CEL as `i.<name>` (or the long form `input.<name>`).

Step inputs come from one of three sources, all populated by the
validator before the step's main work begins:

- **Parsed facts** — values the validator extracts from the submitted
  payload. For example, the EnergyPlus validator parses your IDF and
  exposes facts like `i.zone_count`, `i.idf_version`, `i.north_axis_deg`.
- **Resolved bindings** — for validators that take per-submission
  parameters (FMU model variables, EnergyPlus template variables), the
  values are resolved from your submission data and made available as
  `i.<name>`.
- **Catalog defaults** — values declared by the validator's catalog with
  a default for cases where no parser or binding produces a value.

### Not every validator has step inputs

Step inputs only exist when the validator has a **process** that
transforms data. Schema validators like JSON Schema, XML Schema, and
Basic don't have a transformation process — they just check rules over
the payload directly. For those validators, the `i.*` namespace is
empty, and you write assertions against `p.*` (the payload) and `s.*`
(workflow signals).

If you open a step and its Inputs panel is empty, that's intentional —
the validator you chose doesn't have step inputs.

### Example: parsed step inputs on an EnergyPlus step

After uploading an IDF, the EnergyPlus validator parses it and exposes
facts as step inputs:

```cel
i.idf_version.startsWith("25.")
&& i.zone_count >= 4
```

These assertions can fire **before** the simulation runs — useful for
catching obvious problems quickly without paying for a full
simulation. If the IDF declares zero zones, the simulation isn't
useful; gate on `i.zone_count >= 1` and skip the expensive run.

---

## Step outputs

A **step output** is a value the validator produces after its process
runs. You reference step outputs in CEL as `o.<name>` (or the long form
`output.<name>`).

For advanced validators like EnergyPlus or FMU, step outputs come from
simulation results. For SHACL or THERM, they come from the parser /
evaluator output. For schema validators (JSON Schema, XML Schema,
Basic), there are no step outputs — those validators emit findings
directly, not named output values.

### Stage availability

Step outputs only exist **after** the validator's process completes, so
they can only be referenced in **output-stage assertions**. An attempt
to reference `o.*` in an input-stage assertion will be rejected by the
assertion editor at edit time. The variable autocomplete is filtered
by stage so you only see references that will actually have values.

### Example: EnergyPlus simulation outputs

```cel
o.completed_successfully
&& o.fatal_count == 0
&& o.site_eui_kwh_m2 < s.target_eui
```

Common EnergyPlus step outputs include:

- `site_electricity_kwh` — total electricity consumption
- `site_eui_kwh_m2` — energy use intensity per square meter
- `floor_area_m2` — the simulated floor area
- `unmet_heating_hours` / `unmet_cooling_hours` — comfort metrics

Not every output will be populated for every model. EnergyPlus only
produces a value when the IDF is configured to generate it. If you
reference an output the model didn't produce, Validibot reports it as a
"Value not found" error per the validator's missing-value policy.

### From a downstream step

To reference an earlier step's output from a downstream step, use the
`steps.` namespace:

```cel
steps.energy_check.output.site_eui_kwh_m2 < 100
```

Or — cleaner — promote the output to a signal first and reference it
as `s.<promoted_name>`.

---

## Promotion: lifting step values into the signal vocabulary

If you want a step-local value (a step input or a step output) to be
visible workflow-wide, you **promote** it. The "Copy to Signal" control
on the inputs and outputs tables does this in one click — you give the
value a workflow-wide name, and from that moment it's available as
`s.<your_name>` everywhere downstream.

Promotion is **symmetric** — works the same way for step inputs and
step outputs. Some examples:

- **Output promotion** — an EnergyPlus step produces
  `o.site_eui_kwh_m2`. Promote it to `actual_eui`. Now every downstream
  step references `s.actual_eui` without needing to know which step
  produced it.
- **Input promotion** — an EnergyPlus step parses the IDF and exposes
  `i.zone_count`. Promote it to `zone_count`. Now `s.zone_count` is
  available workflow-wide — useful if a later step also wants to gate
  on this value without re-parsing the IDF.

The original `i.<name>` or `o.<name>` continues to exist step-locally
after promotion; the promoted value is an *additional* accessor in the
workflow signal vocabulary.

---

## The data namespace reference

Here is a quick reference for how to access data in CEL expressions:

| Short form | Long form | What it accesses |
|------------|-----------|------------------|
| `p.key` | `payload.key` | Raw submission data |
| `s.name` | `signal.name` | Workflow signals (from signal mapping or promotion) |
| `i.name` | `input.name` | This step's step inputs (parsed facts, resolved bindings) |
| `o.name` | `output.name` | This step's step outputs (after the validator runs) |
| `steps.step_key.input.name` / `steps.step_key.output.name` | | An earlier step's step inputs and outputs |
| `submission.field` / `submission.metadata.key` | | The submission envelope — submitter metadata and server facts, any file type |

**Which namespaces are populated depends on the validator type:**

| Validator type | `p.*` | `s.*` | `i.*` | `o.*` | `submission.*` |
|---|---|---|---|---|---|
| JSON Schema, XML Schema, Basic | ✅ primary | ✅ if defined | ❌ empty | ❌ empty | ✅ always |
| SHACL, THERM | ✅ available | ✅ if defined | ❌ empty | ✅ primary | ✅ always |
| EnergyPlus, FMU | ❌ payload is opaque | ✅ if defined | ✅ primary at input stage | ✅ primary at output stage | ✅ always |
| Tabular | dataset facts via `i.*` | ✅ if defined | ✅ dataset-level | ✅ dataset-level | ✅ always (except the per-row `row.*` lane) |

`submission.*` is the one namespace populated for **every** validator regardless
of file format — it carries metadata and server facts that live beside the
file, so it works even for non-JSON submissions (RDF `.ttl`, CSV) where `p.*`
is opaque. `s.*` is likewise available everywhere when signals are defined. The
only place `submission.*` is not bound is the Tabular per-row `row.*` loop,
which is intentionally limited to `row`/`s`/`i` for performance.

These prefixes apply to **BASIC and CEL** assertions. SHACL's **SPARQL-ASK**
assertions are a separate type — a raw SPARQL query run in the container against
the RDF `shacl.data`/`shacl.report` graphs — and do **not** use the namespace
prefixes at all (the submission envelope is not in the graph). To gate a SHACL
workflow on submission metadata, use a CEL assertion, not a SPARQL one.

---

## How signal mapping works

Signal mapping happens at the workflow level. When you edit a workflow,
you'll see a section where you can define signals. Each signal has a
name and a data path.

### Setting up a signal

Suppose your submission includes this JSON:

```json
{
  "building_metadata": {
    "name": "Office Tower A",
    "geometry": {
      "total_floor_area_m2": 5000.0,
      "num_floors": 12
    }
  }
}
```

You want a signal called `floor_area` that points to the floor area
value. You'd configure:

- **Signal name**: `floor_area`
- **Data path**: `building_metadata.geometry.total_floor_area_m2`

Now you can write assertions like `s.floor_area > 0` or
`s.floor_area < 50000`, and Validibot automatically resolves
`s.floor_area` to the value `5000.0` by following the data path.

### When are signals resolved?

Signal mapping values are resolved **once**, at the very start of a
validation run, before any workflow step executes. Validibot walks
through each signal mapping, follows the data path into the submission
payload, and stores the resolved value. From that point on, every CEL
expression that references `s.floor_area` reads from that pre-resolved
value — it does not re-traverse the payload each time.

Promoted values (from step inputs or step outputs) are different —
they become available in `s.*` as soon as the producing step's
relevant stage completes.

### When values live in arrays of named objects

Some data formats (like SysML v2, FHIR, or CDA) use arrays where each
element identifies itself with a `name` field rather than being a dict
key. For example:

```json
{
  "ownedAttribute": [
    {"name": "emissivity", "defaultValue": 0.85},
    {"name": "mass", "defaultValue": 3.6}
  ]
}
```

Here, `emissivity` isn't a key you can reach with
`ownedAttribute.emissivity`. Instead, use a **filter expression** in
the data path:

- **Signal name**: `emissivity`
- **Data path**: `ownedAttribute[?@.name=='emissivity'].defaultValue`

For full details on data path syntax, see the
[Data Paths](/app/help/validators/data-paths/) guide.

---

## Putting it all together

Here is a typical workflow for setting up signals, working with step
inputs/outputs, and writing assertions.

### 1. Define your workflow signals

Look at the data your workflow receives and decide which values you'll
need across steps. Create a signal for each one in the workflow's
signal mapping, setting the data path to where the value lives.

### 2. Configure your steps

For each workflow step, choose a validator. If the validator has step
inputs (FMU model variables, EnergyPlus template variables), configure
their bindings — pointing each to either a payload path or a workflow
signal.

### 3. Write assertions

Use signal names, step input names, and step output names in your
assertions:

- Compare a workflow signal against a fixed value: `s.target_eui < 100`
- Compare a step output against a workflow signal:
  `o.site_eui_kwh_m2 < s.target_eui`
- Compare a step input against a workflow signal:
  `i.zone_count >= s.min_zones`
- Compare a step input to a step output (in output-stage assertions):
  `abs(i.expected_floor_area - o.floor_area_m2) < 5.0`

### Example walkthrough

Say you're validating a building energy model with two steps:
**preflight** (lightweight IDF parsing) and **simulation** (full
EnergyPlus run).

**Step 1: workflow signal mapping**

| Signal name | Data path |
|------------|-----------|
| `target_eui` | `metadata.target_eui_kwh_m2` |
| `client_id` | `metadata.client_id` |

**Step 2: preflight step input-stage assertions** (against the parsed
IDF, before simulation runs)

```
i.zone_count >= 4
i.idf_version.startsWith("25.")
i.has_hvac
```

**Step 3: simulation step output-stage assertions** (after simulation
runs)

```
o.completed_successfully
o.fatal_count == 0
o.site_eui_kwh_m2 < s.target_eui
```

**Optional: promote a value if you need cross-step access**

If you want the downstream simulation step to reference the parsed
zone count from preflight, click "Copy to Signal" on `i.zone_count` in
the preflight step's inputs panel and name it `zone_count`. Then any
later step can write `s.zone_count >= 10`.

---

## Related guides

- [Data Paths](/app/help/validators/data-paths/) — full syntax reference
  for JSON dot notation, bracket notation, filter expressions, and XML
  paths
- [CEL Expressions](/app/help/concepts/cel-expressions/) — how to write
  assertion expressions, including the full namespace reference and the
  process-centric explanation of which validators populate which
  namespaces
- [Validators Overview](/app/help/validators/validators-overview/) — how
  validators define their step inputs and step outputs
