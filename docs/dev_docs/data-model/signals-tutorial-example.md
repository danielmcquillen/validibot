# Step Input/Output Tutorial Example

This tutorial walks through a concrete end-to-end example, showing how
the data-layer models work together in the library, on a workflow step,
and at runtime.

Use this guide when you need to answer questions like:

- Where is a step input/output contract defined?
- When is a contract validator-owned versus step-owned?
- Where does the source path live?
- How do defaults and required flags work?
- What records explain what happened during a run?
- How do values flow into CEL via `i.*` and `o.*`?
- How does promotion lift a step-local value to a workflow signal?

> **A note on vocabulary.** This tutorial uses the vocabulary
> established by ADR-2026-05-22b (internal):
> *signal* refers only to workflow-vocabulary values (`s.*`); *step input*
> and *step output* refer to step-local values (`i.*` and `o.*`).
> The Django models match: `StepIODefinition` and `StepInputBinding`
> (the underlying database tables retain legacy names —
> `validations_signaldefinition` and `validations_stepsignalbinding` —
> to avoid a destructive table rename on mature data).

## The Example Workflow

We will use a simple workflow with two validation steps:

1. `envelope_check`
   Uses a shared library validator called `energyplus-envelope-check`.

2. `coil_fmu`
   Uses an FMU uploaded directly to the workflow step.

The submission payload looks like this:

```json
{
  "building": {
    "envelope": {
      "wall_r_value": 18.0,
      "window_u_factor": 0.31
    }
  },
  "hvac": {
    "coil": {
      "inlet_temp_c": 12.0,
      "mass_flow_kg_s": 0.85
    }
  }
}
```

This example shows both ownership modes:

- validator-owned signals for reusable library contracts
- step-owned signals for step-local assets like uploaded FMUs

## The Model Set

The old catalog model was replaced by a normalized set of models with distinct jobs.

| Model | Responsibility | Short version |
| --- | --- | --- |
| `StepIODefinition` | Declares a step input or step output contract | What the input/output is |
| `StepInputBinding` | Wires a step input to a source | Where the step gets it |
| `Derivation` | Computes a value from inputs/outputs | What the system calculates |
| `ResolvedInputTrace` | Records runtime resolution | What actually happened |

If you remember only one mental model, remember this:

- `StepIODefinition` = contract for a step input or step output
- `StepInputBinding` = wiring (inputs only — outputs are produced, not bound)
- `Derivation` = computation
- `ResolvedInputTrace` = audit

## Part 1: Library-Owned Signals

The validator `energyplus-envelope-check` owns these signals:

| Owner | Contract key | Native name | Direction | Type | Meaning |
| --- | --- | --- | --- | --- | --- |
| Validator | `wall_r_value` | `wall_r_value` | input | number | Wall insulation input |
| Validator | `window_u_factor` | `window_u_factor` | input | number | Window thermal transmittance |
| Validator | `annual_site_energy_kwh` | `AnnualSiteEnergy` | output | number | Annual simulated energy use |

These are validator-owned because they belong to the reusable contract of the validator itself. Every workflow step that uses this validator should see the same logical signals.

### `contract_key` vs `native_name`

`contract_key` is the Validibot-facing name:

- CEL expressions
- assertions
- APIs
- stable internal references

`native_name` is the runner-facing name:

- FMU variable names
- EnergyPlus placeholder names
- other provider-native identifiers

They may match, but they do not serve the same purpose.

## Part 2: A Workflow Step Reuses Those Signals

Now add a workflow step named `envelope_check` that uses the shared validator.

The step does not need to create new input definitions. Instead, it reuses the validator-owned `StepIODefinition` rows and adds `StepInputBinding` rows for the workflow-specific wiring.

### Bindings for `envelope_check`

| Workflow step | Signal contract key | Source scope | Source data path | Default | Required |
| --- | --- | --- | --- | --- | --- |
| `envelope_check` | `wall_r_value` | `submission_payload` | `building.envelope.wall_r_value` | none | yes |
| `envelope_check` | `window_u_factor` | `submission_payload` | `building.envelope.window_u_factor` | `0.4` | no |

The contract still lives on `StepIODefinition`, but the wiring now lives on `StepInputBinding`.

That means the same validator can be reused in a different workflow with a different payload shape by changing only the bindings.

## Part 3: A Workflow Step Can Own Its Own Signals

Now look at the second step, `coil_fmu`.

This step uses an FMU uploaded directly to the workflow step. The discovered signals belong only to this step and should not become reusable library-wide signals.

### Step-owned signal definitions for `coil_fmu`

| Owner | Contract key | Native name | Direction | Type | Meaning |
| --- | --- | --- | --- | --- | --- |
| Workflow step `coil_fmu` | `inlet_temp_c` | `T_in` | input | number | Coil inlet temperature |
| Workflow step `coil_fmu` | `mass_flow_kg_s` | `m_dot` | input | number | Coil mass flow |
| Workflow step `coil_fmu` | `cooling_power_kw` | `Q_cool` | output | number | Cooling power result |

### Bindings for `coil_fmu`

| Workflow step | Signal contract key | Source scope | Source data path | Default | Required |
| --- | --- | --- | --- | --- | --- |
| `coil_fmu` | `inlet_temp_c` | `submission_payload` | `hvac.coil.inlet_temp_c` | none | yes |
| `coil_fmu` | `mass_flow_kg_s` | `submission_payload` | `hvac.coil.mass_flow_kg_s` | `1.0` | no |

These signals are step-owned because they came from probing one specific FMU file attached to one specific workflow step.

## Part 4: What the Authoring UI Assembles

The step UI displays one unified signals table, but it is assembled from two sources:

- contract metadata from `StepIODefinition`
- binding metadata from `StepInputBinding`

That is why a single row in the UI can show:

- label
- type
- source/origin
- required
- default value
- source path

The view is unified, but the storage is intentionally split into contract and wiring.

## Part 5: What Happens at Launch Time

When a validation run starts, Validibot resolves input values from the step's `StepInputBinding` rows.

For `envelope_check`, the resolver conceptually does this:

1. Load the step's input bindings.
2. Read each value from the configured `source_scope` and `source_data_path`.
3. Apply `default_value` if the path is missing.
4. Raise a structured error if a required signal cannot be resolved.
5. Build the runner input dict using each signal's `native_name`.

### Resolved inputs for `envelope_check`

From the submission payload above:

- `building.envelope.wall_r_value` -> `18.0`
- `building.envelope.window_u_factor` -> `0.31`

The validator runner receives:

```json
{
  "wall_r_value": 18.0,
  "window_u_factor": 0.31
}
```

### Resolved inputs for `coil_fmu`

From the same submission:

- `hvac.coil.inlet_temp_c` -> `12.0`
- `hvac.coil.mass_flow_kg_s` -> `0.85`

The FMU runner receives:

```json
{
  "T_in": 12.0,
  "m_dot": 0.85
}
```

That split is deliberate:

- resolution looks up `source_data_path`
- execution uses `native_name`

It lets Validibot keep stable internal identifiers while still speaking each engine's native naming scheme.

## Part 6: Defaults and Required Flags

Suppose the submitter omits `hvac.coil.mass_flow_kg_s`.

Because the `coil_fmu` binding says:

- `default_value = 1.0`
- `is_required = false`

resolution still succeeds and the runner receives:

```json
{
  "T_in": 12.0,
  "m_dot": 1.0
}
```

If `inlet_temp_c` is missing, resolution fails before validator execution because that signal is required and has no default.

This is why `StepInputBinding` is not just metadata. It is executable launch-time wiring.

## Part 7: Assertions Reference Step Inputs and Outputs in CEL

Once a step input or step output is declared, it becomes accessible in
CEL expressions through one of two step-local namespaces:

- **Step inputs** appear as `i.<contract_key>` (the `i.*` / `input.*`
  namespace), populated at input stage before the validator container
  runs
- **Step outputs** appear as `o.<contract_key>` (the `o.*` / `output.*`
  namespace), populated at output stage after the container runs

### Input-stage assertion on `envelope_check`

This fires *before* the validator runs. Available namespaces: `p.*`
(raw payload), `s.*` (workflow signals), `i.*` (this step's inputs from
resolved bindings), and `steps.*` (earlier steps).

```cel
i.wall_r_value >= 13 && i.window_u_factor <= 0.5
```

The values come from the `StepInputBinding` resolution: `i.wall_r_value`
was sourced from `p.building.envelope.wall_r_value = 18.0`, so the
assertion sees `18.0 >= 13` and passes.

### Output-stage assertion on `envelope_check`

This fires *after* the validator runs. Adds `o.*` to the available
namespaces.

```cel
o.annual_site_energy_kwh < 50000
```

The value comes from `extract_output_signals()` on the validator,
keyed by the OUTPUT-direction `StepIODefinition`'s `contract_key`.

### Cross-stage assertion on `coil_fmu`

In an output-stage assertion, both `i.*` (resolved before the run) and
`o.*` (produced by the run) are available — useful for comparisons
between configured inputs and computed outputs.

```cel
o.cooling_power_kw > 0 && o.cooling_power_kw < i.mass_flow_kg_s * 10000
```

### Stage-aware authoring

The assertion form rejects `o.*` references in input-stage assertions
at edit time — those outputs don't exist yet. The variable autocomplete
is filtered by stage so authors aren't tempted by references that would
silently resolve to null.

## Part 7b: Promoting Step Inputs and Outputs to Signals

A step-local input or output is only visible within its own step. To
make it accessible workflow-wide — to other steps' assertions, for
example — you **promote** it to a signal by setting its
`promoted_signal_name`.

Promotion is symmetric: it works the same way for both step inputs and
step outputs.

### Output promotion (the existing UI today)

Suppose `envelope_check` produces `o.annual_site_energy_kwh = 42000`,
and the workflow author wants to reference that value in a later step's
assertion. They click "Copy to Signal" on the output's row in the step
UI, give it a workflow-wide name like `envelope_energy`. After
promotion:

- The original `o.annual_site_energy_kwh` still exists, step-locally
- A new `s.envelope_energy = 42000` exists, workflow-wide
- Any downstream step can write `s.envelope_energy < 50000` in CEL

### Input promotion (new under ADR-2026-05-22)

Same mechanism, but on a step input. Suppose `envelope_check` parses
the IDF and exposes `i.zone_count = 12`. A later step wants to gate on
this value too. The author clicks "Copy to Signal" on the input row,
names it `zone_count`. After promotion:

- The original `i.zone_count` still exists, step-locally in
  `envelope_check`
- A new `s.zone_count = 12` exists, workflow-wide
- Any downstream step can write `s.zone_count >= 4` in CEL

Before symmetric promotion, the author would have had to either
re-parse the IDF in the downstream step or reach into
`steps.envelope_check.input.zone_count` (verbose, brittle). Promotion
gives a cleaner workflow-vocabulary name.

### Promotion and the contract layer

Promotion is just a non-empty value in the `promoted_signal_name`
field on the `StepIODefinition` row. The CEL context builder reads
these rows across the workflow (filtered to upstream steps only — the
producing step never sees its own promotion) when assembling the
`s.*` namespace, injecting each promoted value alongside the
workflow-level signals from `WorkflowSignalMapping`.

## Part 8: Derivations Are Separate on Purpose

A derivation is not a raw signal from the submission or a direct output from a runner. It is a computed value.

Example derivation on `coil_fmu`:

| Owner | Contract key | Expression | Type |
| --- | --- | --- | --- |
| Workflow step `coil_fmu` | `specific_cooling_index` | `cooling_power_kw / mass_flow_kg_s` | number |

Signals describe data contracts. Derivations describe computations over those contracts. Keeping them separate makes CEL evaluation and UI behavior much clearer.

## Part 9: Resolved Input Traces Explain the Run

Every time a step resolves inputs, Validibot stores one `ResolvedInputTrace` per input signal.

For `coil_fmu`, the trace rows might look like this:

| Step run | Signal | Source scope used | Source path used | Resolved | Used default | Value snapshot |
| --- | --- | --- | --- | --- | --- | --- |
| run 842 / `coil_fmu` | `inlet_temp_c` | `submission_payload` | `hvac.coil.inlet_temp_c` | yes | no | `12.0` |
| run 842 / `coil_fmu` | `mass_flow_kg_s` | `submission_payload` | `hvac.coil.mass_flow_kg_s` | yes | no | `0.85` |

If a value came from a default, `used_default` would be true. If a required value failed to resolve, the trace would still capture the failure and its error message.

This gives operators a concrete audit trail instead of forcing them to infer how resolution behaved.

## Part 10: Validator-Owned vs Step-Owned Signals

Use validator-owned signal definitions when the signal is part of a reusable validator contract.

Examples:

- shared JSON validator inputs
- stable outputs of a library validator
- library-wide assertion targets

Use workflow-step-owned signal definitions when the signal comes from a step-local asset or step-local customization.

Examples:

- probed FMU variables from a file uploaded to one step
- scanned EnergyPlus template variables on one step
- step-specific custom signals that should not leak back into the validator library

## Part 11: How This Replaced the Old Catalog Model

The old `ValidatorCatalogEntry` model mixed together contract, wiring, display metadata, and runtime behavior.

The new model splits those concerns cleanly:

| Old concern | New home |
| --- | --- |
| Stable identity | `StepIODefinition.contract_key` |
| Input/output direction | `StepIODefinition.direction` |
| Display metadata | `StepIODefinition.label`, `description`, `unit`, `metadata` |
| Provider-specific technical metadata | `StepIODefinition.provider_binding` |
| Runner-facing name | `StepIODefinition.native_name` |
| Submission lookup path | `StepInputBinding.source_data_path` |
| Required/default behavior | `StepInputBinding.is_required`, `default_value` |
| Runtime audit | `ResolvedInputTrace` |

The key design point is that the old catalog model was not replaced by one new model. It was replaced by a set of focused models with clearer responsibilities.

## Part 12: The Debugging Sequence

When something looks wrong, check the system in this order:

1. Does the step input/output contract exist?
   Look for a `StepIODefinition`.

2. Who owns it?
   Validator-owned means shared contract.
   Step-owned means local contract.

3. If it is an input, how is it wired?
   Look for a `StepInputBinding`.

4. If it is computed, is it actually a derivation?
   Look for a `Derivation`.

5. What happened during the run?
   Look for `ResolvedInputTrace` rows.

That sequence maps directly onto the architecture and usually gets you to the right file or table quickly.

## Summary

The unified step-IO model is easiest to understand in layers:

- `StepIODefinition` declares the contract
- `StepInputBinding` wires step inputs to real data
- `Derivation` computes secondary values
- `ResolvedInputTrace` records runtime behavior

Once that clicks, the ownership model becomes straightforward:

- validator-owned signals are reusable contracts
- step-owned signals are local contracts
- bindings are where workflow-specific wiring lives
