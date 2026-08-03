# EnergyPlus Validator

The EnergyPlus Validator checks IDF or epJSON building models and can run a
full EnergyPlus simulation. It exposes parsed model facts under `i.*` and
simulation results and review evidence under `o.*` for CEL assertions.

It reports a **modeled site EUI**. This is not measured or weather-normalized
CBPS WNEUI/EUIt and should not be presented as a compliance value calculated
from utility bills.

## Full simulation and preflight

With **Run EnergyPlus simulation** enabled, the validator requires an EPW
weather file, runs the annual simulation, extracts SQL-backed metrics, and
retains the standard EnergyPlus artifacts.

With that option disabled, it runs conversion-only preflight without requiring
weather. Preflight still reports model, binary, and IDD version evidence and
runs any selected Validibot checks, but SQL-backed simulation metrics are
`null`.

The selected timesteps per hour are applied to a private working copy. The
submitted IDF or epJSON file is never modified.

## Model facts (`i.*`)

Parsed model facts are available before container dispatch, so they can be used
in input-stage assertions:

| Step input | Description |
| --- | --- |
| `i.idf_version` | Version declared by the IDF or epJSON model |
| `i.zone_count` | Number of zones |
| `i.north_axis_deg` | Building north-axis rotation |
| `i.building_name` | Building object name |
| `i.terrain` | Terrain setting |
| `i.solar_distribution` | Solar distribution setting |
| `i.timestep_per_hour` | Timestep declared by the model |
| `i.run_period_count` | Number of run periods |
| `i.surface_count` | Number of building surfaces |
| `i.window_count` | Number of window/fenestration objects |
| `i.construction_count` | Number of Construction objects |
| `i.has_hvac` | Whether the model declares a recognized HVAC object |

Project expectations do not belong in this validator catalog. Put targets such
as maximum EUI or unmet hours in workflow signals (`s.*`) or submission data,
then compare them with EnergyPlus outputs in assertions.

## Review and execution evidence (`o.*`)

The validator reports:

- `o.energyplus_binary_version`, `o.energyplus_binary_build`
- `o.idd_version`, `o.idd_build`, `o.idd_path`
- `o.version_match`
- `o.completed_successfully`, `o.energyplus_returncode`,
  `o.execution_seconds`
- `o.warning_count`, `o.severe_count`, `o.fatal_count`,
  `o.review_issue_count`
- `o.has_sql_output`, `o.has_err_output`, `o.has_csv_output`,
  `o.has_eso_output`

Individual EnergyPlus messages are also preserved. Common version, reference,
schedule, sizing, weather, comfort, output, deprecation, and convergence
problems receive stable review codes and tags. Hiding EnergyPlus warnings only
changes their presentation; it does not change these counts or the run
evidence.

## Simulation metrics (`o.*`)

Energy consumption outputs include electricity, natural gas, district cooling,
district heating, and `o.site_other_fuels_kwh`. The latter combines any other
GJ-valued site fuels, such as propane, fuel oil, coal, or diesel.

`o.site_eui_kwh_m2` is calculated as:

```text
(electricity + natural gas + district cooling + district heating + other fuels)
÷ simulated conditioned area
```

Other outputs include the heating, cooling, lighting, fan, pump, and water
system end uses; unmet heating and cooling hours; peak electric demand;
`o.simulated_conditioned_area_m2`; and optional window heat gain, heat loss,
and transmitted solar metrics.

When EnergyPlus does not produce a declared value, the output is `null` rather
than disappearing. For example, window metrics remain `null` unless the model
requests their `Output:Variable` data.

The derived outputs are:

- `o.total_unmet_hours`
- `o.total_site_energy_kwh`

## Review checks and profiles

The optional Validibot checks are:

- duplicate object names;
- HVAC sizing configuration; and
- seven-day schedule coverage.

The `standard` profile reports missing or mismatched review evidence as
warnings. The opt-in `leed_review` profile promotes required evidence problems
to errors and requires EUI and unmet-hours outputs. It uses the same EnergyPlus
engine and does not claim that a model is LEED compliant.

A useful LEED-readiness baseline is:

```cel
o.completed_successfully
  && o.version_match == true
  && o.fatal_count == 0
  && o.severe_count == 0
  && o.has_sql_output
  && o.site_eui_kwh_m2 != null
  && o.unmet_heating_hours != null
  && o.unmet_cooling_hours != null
```

Keep project and rating-system thresholds in workflow assertions. For example:

```cel
o.site_eui_kwh_m2 != null
  && s.target_modeled_site_eui_kwh_m2 != null
  && o.site_eui_kwh_m2 <= s.target_modeled_site_eui_kwh_m2
```

## Files and artifacts

Full simulation accepts one IDF or epJSON model and one EPW weather file.
Weather can be a workflow resource, a submitted file, or a compatible upstream
artifact. Conversion-only preflight needs only the model.

When produced, SQL, ERR, CSV, and ESO files are exposed as output artifacts for
deeper review. The normalized private model copy is also retained with the run
bundle for debugging.

Template mode resolves submitted parameter values into a private concrete IDF
before dispatch and currently always runs a full simulation.
