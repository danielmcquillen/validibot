# Signals Tutorial Example

This tutorial explains the unified signal architecture with one concrete end-to-end example. It complements the higher-level ADR by showing how the models work together in the library, on a workflow step, and at runtime.

Use this guide when you need to answer questions like:

- Where is a signal contract defined?
- When is a signal validator-owned versus step-owned?
- Where does the source path live?
- How do defaults and required flags work?
- What records explain what happened during a run?

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
| `SignalDefinition` | Declares a signal contract | What the signal is |
| `StepSignalBinding` | Wires an input signal to a source | Where the step gets it |
| `Derivation` | Computes a value from signals | What the system calculates |
| `ResolvedInputTrace` | Records runtime resolution | What actually happened |

If you remember only one mental model, remember this:

- `SignalDefinition` = contract
- `StepSignalBinding` = wiring
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

The step does not need to create new input signal definitions. Instead, it reuses the validator-owned `SignalDefinition` rows and adds `StepSignalBinding` rows for the workflow-specific wiring.

### Bindings for `envelope_check`

| Workflow step | Signal contract key | Source scope | Source data path | Default | Required |
| --- | --- | --- | --- | --- | --- |
| `envelope_check` | `wall_r_value` | `submission_payload` | `building.envelope.wall_r_value` | none | yes |
| `envelope_check` | `window_u_factor` | `submission_payload` | `building.envelope.window_u_factor` | `0.4` | no |

The contract still lives on `SignalDefinition`, but the wiring now lives on `StepSignalBinding`.

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

- signal metadata from `SignalDefinition`
- binding metadata from `StepSignalBinding`

That is why a single row in the UI can show:

- label
- type
- source/origin
- required
- default value
- source path

The view is unified, but the storage is intentionally split into contract and wiring.

## Part 5: What Happens at Launch Time

When a validation run starts, Validibot resolves input values from the step's `StepSignalBinding` rows.

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

This is why `StepSignalBinding` is not just metadata. It is executable launch-time wiring.

## Part 7: Assertions Target Signals

Assertions now conceptually target signal definitions, not legacy catalog rows.

Examples:

- On `envelope_check`, an assertion can target `annual_site_energy_kwh`
- On `coil_fmu`, an assertion can target `cooling_power_kw`

This matters because assertions, CEL context, signal display, and runtime resolution now all use the same contract layer.

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
| Stable signal identity | `SignalDefinition.contract_key` |
| Input/output direction | `SignalDefinition.direction` |
| Display metadata | `SignalDefinition.label`, `description`, `unit`, `metadata` |
| Provider-specific technical metadata | `SignalDefinition.provider_binding` |
| Runner-facing name | `SignalDefinition.native_name` |
| Submission lookup path | `StepSignalBinding.source_data_path` |
| Required/default behavior | `StepSignalBinding.is_required`, `default_value` |
| Runtime audit | `ResolvedInputTrace` |

The key design point is that the old catalog model was not replaced by one new model. It was replaced by a set of focused models with clearer responsibilities.

## Part 12: The Debugging Sequence

When something looks wrong, check the system in this order:

1. Does the signal contract exist?
   Look for a `SignalDefinition`.

2. Who owns it?
   Validator-owned means shared contract.
   Step-owned means local contract.

3. If it is an input, how is it wired?
   Look for a `StepSignalBinding`.

4. If it is computed, is it actually a derivation?
   Look for a `Derivation`.

5. What happened during the run?
   Look for `ResolvedInputTrace` rows.

That sequence maps directly onto the architecture and usually gets you to the right file or table quickly.

## Summary

The unified signal model is easiest to understand in layers:

- `SignalDefinition` declares the contract
- `StepSignalBinding` wires step inputs to real data
- `Derivation` computes secondary values
- `ResolvedInputTrace` records runtime behavior

Once that clicks, the ownership model becomes straightforward:

- validator-owned signals are reusable contracts
- step-owned signals are local contracts
- bindings are where workflow-specific wiring lives
