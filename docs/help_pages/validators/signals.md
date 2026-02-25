# Signals

Signals are named values that flow through a validation run. They connect the data in your submission to the rules you write, so you never have to hard-code paths into raw payloads. Instead of writing a rule that says "check the value at `results.annual_energy.site_eui_kwh_m2`," you define a signal called `site_eui_kwh_m2` and then write `site_eui_kwh_m2 < 100`.

---

## What is a signal?

A signal is a named piece of data that a validator either **consumes** (input) or **produces** (output). Every signal has a few key properties:

| Property | What it does |
|----------|-------------|
| **Name (slug)** | The identifier you use in assertions and CEL expressions, like `floor_area` or `site_eui_kwh_m2`. |
| **Data path** | Where to find the value in the actual data. For example, `building_metadata.geometry.total_floor_area_m2`. |
| **Stage** | Whether the signal is an **input** (available before the validator runs) or an **output** (produced by the validator). |
| **Data type** | The kind of value: Number, String, Boolean, Timeseries, or Object. |
| **Required** | Whether the signal must be present. Missing required signals are flagged as errors. |

---

## Input vs output signals

### Input signals

Input signals represent values that are available **before** the validator runs. They typically come from metadata that the submitter provides when they launch a validation, or from fields in the submission data itself.

For example, an EnergyPlus validator might define these input signals:

- `expected_floor_area_m2` -- the floor area the submitter says the building should have
- `target_eui_kwh_m2` -- the energy use intensity target for compliance
- `max_unmet_hours` -- the maximum allowable unmet comfort hours

You can write assertions against input signals to catch problems early, even before the validator runs. For instance, `expected_floor_area_m2 > 0` ensures the submitter provided a valid floor area.

### Output signals

Output signals represent values the validator **produces** during execution. For advanced validators like EnergyPlus or FMU, these come from the simulation results. For built-in validators, outputs can be populated directly from the submission data.

EnergyPlus output signals include things like:

- `site_electricity_kwh` -- total electricity consumption
- `site_eui_kwh_m2` -- energy use intensity per square meter
- `floor_area_m2` -- the simulated floor area
- `unmet_heating_hours` / `unmet_cooling_hours` -- comfort metrics

Output signals are what you typically write assertions against. A rule like `site_eui_kwh_m2 < target_eui_kwh_m2` compares an output signal against an input signal.

---

## How the data path connects signals to data

The **data path** is the bridge between a signal's name and the actual value in your data. The signal name (`floor_area`) is what you use in assertions. The data path (`building_metadata.geometry.total_floor_area_m2`) tells Validibot where to find that value.

### Example

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

You want to create a signal called `floor_area` that points to the floor area value. You'd configure:

- **Signal name**: `floor_area`
- **Data path**: `building_metadata.geometry.total_floor_area_m2`

Now you can write assertions like `floor_area > 0` or `floor_area < 50000`, and Validibot automatically resolves `floor_area` to the value `5000.0` by following the data path.

### When the data path matches the slug

If the value you need is a top-level key in the data and its name matches your signal slug, the data path can simply be the slug itself. For example, if your data looks like `{"temperature": 21.3}` and your signal is named `temperature`, the data path is just `temperature`.

For full details on data path syntax (dot notation, bracket notation for arrays, XML paths), see the [Data Paths](/app/help/validators/data-paths/) guide.

---

## Putting it all together

Here is a typical workflow for setting up signals and writing assertions:

### 1. Define your signals

Look at the data your validator works with and decide what values you want to check. Create a signal for each one, choosing a clear slug and setting the data path to where the value lives.

### 2. Set data paths

For each signal, set the data path to the location in the data. Use dot notation for nested JSON (e.g., `results.energy.total_kwh`).

### 3. Write assertions

Use signal names in your assertion expressions. You can compare signals against fixed values (`site_eui_kwh_m2 < 100`), against each other (`floor_area_m2 == expected_floor_area_m2`), or use CEL functions for more advanced checks.

### Example walkthrough

Say you're validating a building energy model. You define:

| Signal name | Stage | Data path | Data type |
|------------|-------|-----------|-----------|
| `expected_floor_area_m2` | Input | `metadata.floor_area` | Number |
| `site_eui_kwh_m2` | Output | `results.site_eui_kwh_m2` | Number |
| `floor_area_m2` | Output | `results.floor_area_m2` | Number |

Then you write assertions:

- `expected_floor_area_m2 > 0` -- validates that the submitter provided a floor area (runs before the simulation)
- `site_eui_kwh_m2 < 150` -- checks that the simulated EUI is within range (runs after the simulation)
- `floor_area_m2 == expected_floor_area_m2` -- verifies the simulation used the correct floor area

---

## Related guides

- [Data Paths](/app/help/validators/data-paths/) -- full syntax reference for JSON dot notation, bracket notation, and XML paths
- [CEL Expressions](/app/help/concepts/cel-expressions/) -- how to write assertion expressions using signal values
- [Validators Overview](/app/help/validators/validators-overview/) -- how validators define and use signals
