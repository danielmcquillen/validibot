# ADR-00XX: Introduce FMI-Based Validator and Modal.com Execution

## Status

Proposed (2025-11-17)

## Context

SimpleValidations is evolving from “schema-only” validations (JSON Schema, XML Schema, CEL-based checks) toward richer, simulation-backed validations. Many engineering-oriented users model system behavior using the Functional Mock-up Interface (FMI) standard and export models as FMUs (Functional Mock-up Units).

There is a strong use case for allowing workflow authors to:

- Upload an FMU (FMI 2.0/3.0),
- Automatically discover its inputs and outputs,
- Bind those inputs to submission fields / derived signals,
- Run the FMU as part of a validation workflow step, and
- Use the resulting outputs in CEL-based assertions.

However, FMUs are effectively untrusted native code wrapped in a zip. Running them directly in the SimpleValidations web/app infrastructure would significantly increase security risk. FMUs may also be designed for real-time or long-running co-simulation, which does not fit the “short, one-shot validation” model.

We need a design that:

- Exposes FMI-based validation in a simple way to workflow authors,
- Automatically surfaces FMU IO as Catalog entries / signals for CEL,
- Uses a safe execution environment (Modal.com) for untrusted FMUs,
- Enforces constraints on which FMUs are acceptable (e.g., short runs, limited resource usage),
- Keeps the core Django app stateless with respect to simulation.

## Decision

We will:

1. **Introduce an “FMI Validator” step type** that runs an FMU as part of a workflow via Modal.com.
2. **Store FMUs as versioned assets (e.g., in S3) and never execute them on the web/app servers.**
3. **Introspect FMUs automatically** (via `modelDescription.xml`) to detect variables and wire them into the Validator Catalog.
4. **Execute FMUs only in a sandboxed Modal.com container** with strict limits (CPU, memory, runtime) and no access to core infra or secrets.
5. **Apply safety checks** (size, metadata, short “probe run”) before marking FMUs as approved for use in workflows.
6. **Constrain the usage semantics** to short, one-shot simulations that behave like pure-ish functions `(inputs, config) → outputs`.

### 1. FMI Validator Step Type

We introduce a new Validator step type, conceptually:

- **Name:** `FMIValidatorStep`
- **Purpose:** run a configured FMU with bound inputs, obtain outputs, and make them available as signals for CEL checks.

**Data model alignment**

- We will add a new `ValidationType` value (for example `FMI`) and model instances of `Validator`/`Ruleset` using the existing tables. The workflow author experience should look like adding any other validator step.
- `FMIModel` objects are scoped to a `Project` (in the same org) and referenced by the validator config; WorkflowSteps remain the linking table between the workflow and the validator.
- Role requirements follow the platform rules: `OWNER/ADMIN/AUTHOR` can upload/manage FMUs and configure the validator; `EXECUTOR` can run workflows but only see their own run results; `RESULTS_VIEWER` can read results across the org.

Key configuration fields (stored as JSON/config model):

- `fmi_model_id` – reference to the uploaded FMU (`FMIModel`).
- `input_bindings` – mapping from FMU input variable names to source expressions, e.g.:

  ```json
  {
    "T_amb": "payload.weather.outdoor_temp",
    "T_set": "signal:desired_setpoint"
  }
  ```

- `output_bindings` – list or mapping of FMU output names to Catalog/Signal identifiers, e.g.:

  ```json
  {
    "T_zone": "fmi_result.T_zone",
    "P_heating": "fmi_result.P_heating"
  }
  ```

- `simulation_config` – basic numeric settings:

  ```json
  {
    "start_time": 0.0,
    "stop_time": 3600.0,
    "max_step_size": 60.0
  }
  ```

At runtime, the workflow engine will:

1. Resolve `input_bindings` against the current submission/context.
2. Call the Modal function with: `(fmu_storage_key, inputs, simulation_config, output_variable_names)`.
3. Receive a dict of outputs.
4. Register outputs as signals using the configured `output_bindings`.
5. Continue to downstream CEL-based validations that can reference those signals.

### 2. FMU Storage and Metadata

We introduce an `FMIModel` entity:

- Stores metadata about the FMU and where it lives (e.g., S3 key).
- Tracks whether it is approved for workflow use.

Example fields:

- `project` – FK to `Project`.
- `name` – human name.
- `file` – `FileField` or S3 path, e.g. `fmi/{project_id}/{uuid}.fmu`.
- `fmi_version` – string (“2.0”, “3.0”).
- `kind` – “ModelExchange” or “CoSimulation”.
- `is_approved` – boolean indicating whether the FMU passed safety checks.
- `introspection_metadata` – JSON (e.g., list of variables, capabilities, etc.).

FMUs are uploaded through the SimpleValidations UI, stored in a bucket, and referenced by key. The FMU file is never extracted or executed on Django/gunicorn nodes.

### 3. Automatic Introspection of Variables → Catalog

Upon upload (or via a background task), we will:

1. Open the FMU as a zip and parse `modelDescription.xml`.
2. Enumerate `ScalarVariable` elements and capture:
   - `name`
   - `causality` (input, output, parameter, local, etc.)
   - `variability`
   - `valueReference`
   - type information (Real, Integer, Boolean, String, Enumeration)
   - units (for Real variables)
3. Persist this in an `FMIVariable` model tied to `FMIModel`.

Example `FMIVariable` fields:

- `fmi_model` – FK → `FMIModel`
- `name`
- `causality`
- `variability`
- `value_reference`
- `value_type` (Real/Integer/Boolean/String/Enumeration)
- `unit`
- `catalog_entry` – optional FK → `CatalogEntry` (for IO that we expose)

We will then **offer the author a selection screen** to choose which variables should be exposed to the Validator Catalog:

- For `causality = input` → candidate input signals.
- For `causality = output` → candidate output signals.

For each selected variable, we auto-create a `CatalogEntry` (or equivalent) so it appears in the Workflow + CEL authoring experience under consistent names.

This allows workflow authors to:

- Bind inputs from submissions and other signals to FMU inputs.
- Use FMU outputs in CEL expressions as first-class signals.

### 4. Execution in Modal.com

All FMU execution happens in Modal.com. We will define one or more Modal functions with:

- A minimal Docker image including `fmpy` (or other FMI runtime) and S3 client.
- Non-root user.
- Tight resource limits (CPU, memory, wall-clock timeout).
- No outbound network access (or extremely restricted).
- No access to application secrets beyond what’s needed to fetch the FMU.

A typical function signature:

```python
run_fmu_validation(
    fmu_s3_key: str,
    inputs: dict[str, float | int | bool | str],
    simulation_config: dict,
    output_variables: list[str]
) -> dict[str, float | int | bool | str]
```

Execution flow in Modal:

1. Download FMU to `/tmp/model.fmu` from S3.
2. Perform basic integrity checks (size, zip sanity).
3. Use `fmpy` or similar to:
   - Read `modelDescription.xml` (as needed).
   - Run a simulation from `start_time` to `stop_time` with provided inputs.
4. Extract last values (or specified time slices) for `output_variables`.
5. Return outputs as a JSON-serializable dict.

The Django workflow engine will invoke this Modal function asynchronously/synchronously (depending on the existing task model), treat errors/timeouts as validation failures or internal errors, and log outcomes for observability.

### 5. Safety, Suitability, and Approval Flow

Because FMUs are untrusted native code, we will apply multiple layers of protection:

#### 5.1. Upload-Time Checks (Django)

- **Size limit**: reject FMUs above a configured threshold (e.g. 50–100 MB).
- **File type check**: ensure the file is a valid zip with `modelDescription.xml`.
- **Basic FMI version/kind check**:
  - Only accept supported FMI versions (starting with 2.0, 3.0).
  - Optionally restrict to either ModelExchange or CoSimulation depending on what the runtime supports.

FMUs that pass these checks are stored but **not yet approved** for use.

#### 5.2. Probe Run (Modal)

We will run a short “probe” simulation:

- Very small horizon (e.g., 0 → 1 s model time).
- Default or simple inputs.
- Short timeout (e.g., 5–10 seconds wall-clock).

The probe will:

- Verify that the FMU can be instantiated and stepped.
- Estimate resource usage and rough performance.
- Fail early for FMUs that crash, hang, or demand unsupported capabilities.

If the probe passes, the `FMIModel` is flagged `is_approved = True`. Otherwise, it remains unapproved and is unavailable to workflow steps (with a clear message to the author).

#### 5.3. Runtime Guards (Modal)

For each validation run:

- Enforce strict CPU, memory, and timeout limits.
- Use a “pure function” style driver: no persistent state between runs.
- Avoid mounting host volumes or exposing internal network.

If a run exceeds limits or fails, the workflow step will:

- Mark the validation step as errored.
- Capture structured logs for debugging.
- Optionally allow the org to see that “FMU X is unreliable for the selected horizon/inputs.”

### 6. Usage Semantics: Short, One-Shot Validations Only

We will explicitly scope FMI-based validation to **short, self-contained simulations** that behave roughly like functions:

> `(inputs, simulation_config) → outputs`

We **will not** support:

- Real-time co-simulation that needs tight external stepping loops aligned with wall-clock time.
- Long-running simulations meant to run for minutes or hours of real time per validation.
- FMUs that depend on external resources (files, network, external processes).

This scope will be communicated in UI copy and documentation, as well as enforced via timeouts and probe runs.

## Consequences

### Positive

- **Rich, simulation-based validation** becomes possible in SimpleValidations using a standard (FMI).
- **Workflow authors get a clean UX**:
  - Upload FMU.
  - Pick inputs/outputs from an auto-detected list.
  - Bind to existing signals and reference results in CEL.
- **Security and isolation are improved** by never executing FMUs on the core web/app nodes and leveraging Modal’s sandboxing.
- **Architecture stays clean**:
  - FMU execution is just another external compute step, like other heavy simulations.
  - Django remains orchestration + configuration only.

### Negative / Trade-offs

- **Increased complexity and operational cost**:
  - Requires Modal infrastructure, monitoring, and cost management.
  - Adds a new external dependency (`fmpy` or equivalent).
- **Performance constraints**:
  - Some FMUs may be too slow or heavy for the one-shot, short-horizon model.
  - Authors may be confused when their existing “big” FMUs fail probe runs.
- **User education required**:
  - Clear documentation on what FMI versions/kinds are supported.
  - Guidance for building “validation-friendly” FMUs (short runs, stable behavior).

### Alternatives Considered

1. **Run FMUs directly on the Django/gunicorn/Celery infrastructure**

   - Rejected due to security risk: FMUs can contain arbitrary native code.
   - Would increase blast radius of a malicious FMU.

2. **Disallow FMI completely and only allow scripted validators (e.g. Python CEL wrappers)**

   - Simpler, but loses interoperability with existing engineering tools.
   - Many users already have FMUs; requiring rewrites would reduce adoption.

3. **Pre-approve only a curated set of FMUs (no user uploads)**

   - Safer but less flexible.
   - Could be a future “managed library” offering, but we still need user-uploads for generality.

4. **Use a different execution backend (self-managed cluster, other cloud service)**

   - Modal.com is chosen for now due to:
     - Good fit with short-lived, containerized compute.
     - Familiarity and reuse with other SimpleValidations simulation work.
   - Backend can be revisited later if requirements or pricing change.

## Implementation Notes / Next Steps

1. **Data model**

   - Add `FMIModel` and `FMIVariable` models (scoped to `Project`/`Org`).
   - Add optional relationships to `CatalogEntry`.
   - Add `FMIValidatorStep` config model/type using the existing `Validator`/`WorkflowStep` pattern and a new `ValidationType` enum value.

2. **Upload & introspection**

   - Implement FMU upload API + UI.
   - Implement introspection job to parse `modelDescription.xml`.
   - Implement author UI to select which variables become Catalog entries.

3. **Modal integration**

   - Create Modal image with FMI runtime (`fmpy`).
   - Implement `probe_fmu` and `run_fmu_validation` functions.
   - Add timeouts and resource limits.

4. **Workflow engine integration**

   - Implement `FMIValidatorStep.run()` that:
     - Resolves inputs.
     - Calls Modal.
     - Registers outputs as signals.
   - Add logging and error handling.

5. **Documentation & UX**
   - Document FMI support, limitations, and best practices.
   - Explain approval flow and error messages for “unsuitable” FMUs.
