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
| `run_stage` | `INPUT` (available before the engine runs) or `OUTPUT` (produced by the engine). |
| `data_type` | Value type: `NUMBER`, `STRING`, `BOOLEAN`, `TIMESERIES`, or `OBJECT`. |
| `target_field` | Path used to locate the value in the payload or processor output. |
| `input_binding_path` | Optional path to the submission field for input signals. |
| `binding_config` | Provider-specific hints (e.g., EnergyPlus meter name, derivation expression). |
| `is_required` | Whether the signal must be present. Missing required signals evaluate to `null`. |
| `is_hidden` | Hidden from authoring UI but still available to CEL expressions. |

See [Validators](validators.md) for how catalog entries are defined and synced.

## Signal types

### Input signals

Input signals represent values available *before* the validator runs. They
typically come from submission metadata or form fields provided at launch time.

For example, EnergyPlus defines these input signals:

- `expected_floor_area_m2` - User-provided floor area for comparison
- `target_eui_kwh_m2` - Target energy use intensity for compliance checking
- `max_unmet_hours` - Maximum allowable unmet hours threshold

Input signals let authors write assertions against what the submitter *claims*,
before the engine even runs. An input assertion like
`expected_floor_area_m2 > 0` catches bad metadata early.

### Output signals

Output signals represent values the validator *produces* during execution. For
advanced validators (EnergyPlus, FMI), these are extracted from the container's
output envelope. For built-in validators, the engine can populate them directly.

EnergyPlus output signals include metrics like:

- `site_electricity_kwh` - Total electricity consumption
- `site_natural_gas_kwh` - Total gas consumption
- `site_eui_kwh_m2` - Energy use intensity per square meter
- `floor_area_m2` - Simulated floor area
- `unmet_heating_hours` / `unmet_cooling_hours` - Comfort metrics

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

## How signals flow during execution

### Within a single step

1. **CEL context building.** The engine calls `_build_cel_context()`, which
   queries the validator's catalog entries and resolves each slug against the
   payload. Input signals are resolved from submission metadata. Output signals
   are resolved from the validator's output envelope.

2. **Assertion evaluation.** CEL expressions reference signals by slug. The
   expression `site_eui_kwh_m2 < 100` looks up `site_eui_kwh_m2` in the
   context dictionary built in step 1.

3. **Signal extraction.** The engine returns extracted signals in
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

3. **Context injection.** The collected signals are passed to the engine via
   `RunContext.downstream_signals`, then exposed in the CEL context under a
   `steps` namespace:

   ```cel
   steps.<step_run_id>.signals.<slug>
   ```

This lets a downstream step write assertions that reference outputs from an
earlier step. For example, a compliance-checking step could assert that the
EnergyPlus step's output meets a threshold.

## Storage

Signals are not stored in a dedicated table. They live in the `summary` JSONField
on `ValidationRun`, nested under `steps.<step_run_id>.signals`. This keeps
signal storage lightweight (no extra rows per signal per run) and naturally
scoped to the run lifecycle.

The `store_signals()` method on `ValidationStepProcessor` handles persistence:

```python
summary = self.validation_run.summary or {}
steps = summary.setdefault("steps", {})
step_key = str(self.step_run.id)
step_data = steps.setdefault(step_key, {})
step_data["signals"] = signals
self.validation_run.summary = summary
self.validation_run.save(update_fields=["summary"])
```

## Seed data

Advanced validators define their signals as seed data in
`validibot/validations/seeds/`. The `sync_advanced_validators` management command
syncs these to the database. Seed data must match the field names in the
corresponding shared library models (e.g.,
`validibot_shared.energyplus.models.EnergyPlusSimulationMetrics`).

See [Validators - Advanced validator seed data](validators.md#advanced-validator-seed-data)
for more on the sync process.

## Related documentation

- [Validators](validators.md) - Catalog entry model and seed data
- [Assertions](assertions.md) - How signals are referenced in rules
- [Step Processor](../overview/step_processor.md) - Signal extraction and storage implementation
- [Results](results.md) - How signal values appear in run summaries
