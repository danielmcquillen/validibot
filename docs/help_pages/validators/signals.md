# Signals, Inputs, and Outputs

Validibot organizes data into three distinct concepts: **signals**, **validator inputs**, and **validator outputs**. Understanding the difference between them is key to writing clear workflows and assertions.

---

## Signals

A signal is an author-defined named value. You create signals to give meaningful names to the data your workflow cares about, so that your assertions can reference `s.floor_area` instead of a raw path like `p.building_metadata.geometry.total_floor_area_m2`.

There are two ways to create signals:

**Signal mapping** -- On the workflow-level page, you define a signal by giving it a name and a data path. This maps a raw submission value to a clean, reusable name. For example, you might map the signal `floor_area` to the data path `building_metadata.geometry.total_floor_area_m2`.

**Output promotion** -- When a validator produces an output, you can promote it to a signal so that downstream steps (and assertions) can reference it by a stable name. If an EnergyPlus step produces `output.site_eui_kwh_m2`, you can promote that to a signal called `site_eui` so downstream steps reference it as `s.site_eui`.

Once a signal exists, you reference it in CEL expressions as `s.name` (or the long form `signal.name`). Signals are the primary way you should reference data in assertions -- they keep your expressions readable and insulate them from changes to the underlying data structure.

Remember that in CEL expressions you always use `p.` (or `payload.`) to reference raw submission data and `s.` (or `signal.`) to reference a signal. So `p.price` reads the `price` field directly from the submission payload, while `s.price` reads a signal you've defined called `price`. The prefix makes it clear where the value comes from.

### Why signals matter -- a before-and-after example

To see why signals are worth the effort, consider a realistic building energy submission. The data arrives as a deeply nested JSON payload because it follows an industry schema:

```json
{
  "project": {
    "id": "PRJ-2024-0142",
    "building": {
      "geometry": {
        "gross_floor_area_m2": 4800.0,
        "conditioned_floor_area_m2": 4200.0,
        "num_stories_above_grade": 3
      },
      "envelope": {
        "walls": [
          { "id": "wall-north", "u_value_w_m2k": 0.27, "area_m2": 540.0 },
          { "id": "wall-south", "u_value_w_m2k": 0.27, "area_m2": 380.0 }
        ],
        "roof": { "u_value_w_m2k": 0.18, "area_m2": 1600.0 }
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

**Without signals**, your CEL assertions reference the raw paths directly:

```
p.project.building.geometry.gross_floor_area_m2 > 0
```
```
output.site_eui_kwh_m2 < p.project.compliance.targets.max_site_eui_kwh_m2
```
```
output.unmet_hours < p.project.compliance.targets.max_unmet_hours
```
```
p.project.building.envelope.walls.all(w,
    w.u_value_w_m2k <= 0.35)
```

These work, but they're fragile. If the submitter's schema changes `compliance.targets` to `compliance.performance_targets`, every assertion that references it breaks. They're also hard to read -- the business intent ("is the EUI within the target?") is buried under path navigation.

**With signals**, you define the mapping once and write clean assertions:

| Signal name | Data path |
|------------|-----------|
| `floor_area` | `project.building.geometry.gross_floor_area_m2` |
| `target_eui` | `project.compliance.targets.max_site_eui_kwh_m2` |
| `max_unmet_hours` | `project.compliance.targets.max_unmet_hours` |

Now your assertions become:

```
s.floor_area > 0
```
```
output.site_eui_kwh_m2 < s.target_eui
```
```
output.unmet_hours < s.max_unmet_hours
```

The business logic is immediately clear. If the data structure changes, you update the data path in the signal mapping -- the assertions stay exactly the same. If you add a second workflow that validates the same data with different rules, it can reuse the same signal names.

---

## Validator Inputs

Validator inputs are what a validator needs in order to run. They are not the same thing as signals -- they are the validator's own requirements.

Each validator defines its own set of inputs. For example, an EnergyPlus validator might need these inputs:

- `expected_floor_area_m2` -- the floor area the submitter says the building should have
- `target_eui_kwh_m2` -- the energy use intensity target for compliance
- `max_unmet_hours` -- the maximum allowable unmet comfort hours

When you add a validator to a workflow step, you bind its inputs to signals or data paths. The binding tells Validibot where to find each value the validator requires. You might bind the validator's `expected_floor_area_m2` input to the signal `s.floor_area`, or directly to a payload path like `p.metadata.floor_area`.

The distinction matters: the validator defines what it needs (inputs), and you decide where those values come from (signals or payload paths). This separation means you can reuse the same validator across different workflows, binding its inputs to different data each time.

---

## Validator Outputs

Validator outputs are the values a validator produces after it runs. For advanced validators like EnergyPlus or FMU, these come from simulation results. For built-in validators, outputs can be populated directly from the submission data.

Within the current step's assertions, you reference outputs as `output.name` (or the short form `o.name`). For example, after an EnergyPlus simulation runs, you might write an assertion like `output.site_eui_kwh_m2 < 150`.

From a downstream step, you reference another step's outputs using the `steps.` namespace: `steps.energy_check.output.site_eui_kwh_m2`.

EnergyPlus output examples include things like:

- `site_electricity_kwh` -- total electricity consumption
- `site_eui_kwh_m2` -- energy use intensity per square meter
- `floor_area_m2` -- the simulated floor area
- `unmet_heating_hours` / `unmet_cooling_hours` -- comfort metrics

Not every output will be populated for every model. EnergyPlus only produces a value when the IDF is configured to generate it. If you reference an output that the model doesn't produce, Validibot will report it as a "Value not found" error.

**Promoting outputs to signals.** If you want downstream steps to reference a validator output by a stable name, promote it to a signal. This makes the value available as `s.name` throughout the rest of the workflow, rather than requiring downstream steps to know which step produced it.

---

## The data namespace reference

Here is a quick reference for how to access data in CEL expressions:

| Short form | Long form | What it accesses |
|------------|-----------|------------------|
| `p.key` | `payload.key` | Raw submission data |
| `s.name` | `signal.name` | Author-defined signals (from signal mapping or promoted outputs) |
| `output.name` | `o.name` | This step's validator outputs |
| `steps.step_key.output.name` | | Upstream step outputs |

---

## How signal mapping works

Signal mapping happens at the workflow level. When you edit a workflow, you'll see a section where you can define signals. Each signal has a name and a data path.

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

You want a signal called `floor_area` that points to the floor area value. You'd configure:

- **Signal name**: `floor_area`
- **Data path**: `building_metadata.geometry.total_floor_area_m2`

Now you can write assertions like `s.floor_area > 0` or `s.floor_area < 50000`, and Validibot automatically resolves `s.floor_area` to the value `5000.0` by following the data path.

### When are signals resolved?

Signals are resolved **once**, at the very start of a validation run, before any workflow step executes. Validibot walks through each signal mapping, follows the data path into the submission payload, and stores the resolved value. From that point on, every CEL expression that references `s.floor_area` reads from that pre-resolved value -- it does not re-traverse the payload each time. This means signal values are consistent across all steps in a run, and the resolution cost is paid only once.

### When the data path is simple

If the value you need is a top-level key in the data and its name matches your signal, the data path can simply be the key name. For example, if your data looks like `{"temperature": 21.3}` and your signal is named `temperature`, the data path is just `temperature`.

### When values live in arrays of named objects

Some data formats (like SysML v2, FHIR, or CDA) use arrays where each element identifies itself with a `name` field rather than being a dict key. For example:

```json
{
  "ownedAttribute": [
    {"name": "emissivity", "defaultValue": 0.85},
    {"name": "mass", "defaultValue": 3.6}
  ]
}
```

Here, `emissivity` isn't a key you can reach with `ownedAttribute.emissivity`. Instead, use a **filter expression** in the data path to find the right element:

- **Signal name**: `emissivity`
- **Data path**: `ownedAttribute[?@.name=='emissivity'].defaultValue`

The `[?@.name=='emissivity']` part means "find the array element where `name` equals `emissivity`." You can chain filters for deeply nested structures:

```
ownedMember[?@.name=='RadiatorPanel'].ownedAttribute[?@.name=='emissivity'].defaultValue
```

Once the signal is wired up, you write assertions exactly the same way -- `s.emissivity > 0.0 && s.emissivity <= 1.0`. Validibot resolves the signal through the filter expression and makes the value available by its signal name.

For full details on data path syntax (dot notation, bracket notation, filter expressions, XML paths), see the [Data Paths](/app/help/validators/data-paths/) guide.

---

## Putting it all together

Here is a typical workflow for setting up signals, binding inputs, and writing assertions.

### 1. Define your signals

Look at the data your workflow receives and decide which values you'll need across steps. Create a signal for each one in the workflow's signal mapping, setting the data path to where the value lives.

### 2. Bind validator inputs

For each workflow step, bind the validator's inputs to your signals or directly to payload paths. This tells the validator where to find the data it needs.

### 3. Write assertions

Use signal names and output names in your assertions. You can compare signals against fixed values (`s.site_eui_kwh_m2 < 100`), compare outputs against signals (`output.floor_area_m2 == s.expected_floor_area`), or use CEL functions for more advanced checks.

### Example walkthrough

Say you're validating a building energy model.

First, you define signals in the workflow's signal mapping:

| Signal name | Data path |
|------------|-----------|
| `expected_floor_area` | `metadata.floor_area` |
| `target_eui` | `metadata.target_eui_kwh_m2` |

Then, in your EnergyPlus step, the validator produces outputs like `site_eui_kwh_m2` and `floor_area_m2`.

Now you write assertions:

- `s.expected_floor_area > 0` -- validates that the submitter provided a floor area
- `output.site_eui_kwh_m2 < s.target_eui` -- checks that the simulated EUI is within the target
- `output.floor_area_m2 == s.expected_floor_area` -- verifies the simulation used the correct floor area

If you promote `output.site_eui_kwh_m2` to a signal called `actual_eui`, downstream steps can reference it as `s.actual_eui` without needing to know which step produced it.

---

## Related guides

- [Data Paths](/app/help/validators/data-paths/) -- full syntax reference for JSON dot notation, bracket notation, filter expressions, and XML paths
- [CEL Expressions](/app/help/concepts/cel-expressions/) -- how to write assertion expressions, including the full namespace reference
- [Validators Overview](/app/help/validators/validators-overview/) -- how validators define and use inputs and outputs
