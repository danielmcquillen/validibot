# ADR-0021: CEL Rulesets, Provider Selection, and Execution Model

**Date:** 2025-10-31  
**Status:** Proposed (seeking acceptance)  
**Owners:** Platform / Validations

## Context

SimpleValidations runs **workflows** composed of **steps**. Each step executes a **Validation** which uses a
**Validator Engine** (registered in code) and may reference a **Ruleset** (author-defined checks).

We need to extend this pattern to allow the workflow author to create a rich set of definitions of the types of
validations checks that will be involved this particular validation.

We need:

- A general, reusable **ruleset** format for many domains (EnergyPlus, Modelica, WinCalc, etc.).
- A way to extend this **ruleset** over time to accomodate (with further definitions) other as yet unknown domains.
- A safe, declarative way to express numeric/logical checks.
- A clean way to bind these checks to the domain-specific data/functions (e.g., `series()` for timeseries) that actually perform the check.
- Version-accurate behavior (E+ 23.x vs 24.x) without putting brittle dotted paths in the DB.
- Fast evaluation across many runs (avoid reparsing expressions every time).

We chose **CEL** (Common Expression Language) for the ruleset’s expressions, plus **domain Providers** that expose CEL helpers and validate domain config. Rulesets now own explicit **Assertions** (previously called “checks”) so that ruleset revisions can bundle multiple reusable rules without mutating the workflow step itself.

## Decision

1. **Validator is the single source of truth for provider version.**

   - The **WorkflowStep** selects a **Validator** row (e.g., `ENERGYPLUS v23.2`).
   - The engine resolves a **Provider** implementation **in code** via a registry keyed by `(validation_type, validator.version)`.
   - **Rulesets do not store provider class names or versions.**

2. **Ruleset format (v1) is engine-agnostic and tiny but assertion-aware:**

   - `signals` (named values bound into CEL), `derivations` (CEL expressions), and `assertions` (CEL booleans with messages) each live in their own catalog so UI and engines can reason about them separately.
   - `signals` are split into **inputs** (available before execution) and **outputs** (materialised by the provider after the run). Providers can pre-populate either side with defaults for their domain (for example, EnergyPlus publishes standard meter outputs; a generic Modelica validator requires the author to name them).
   - Assertions are persisted as first-class rows (`RulesetAssertion`) linked to the Ruleset rather than directly to the `WorkflowStep`. Swapping the ruleset swaps all assertions without editing the step.
   - A small `provider` block remains for non-version options (e.g., instrumentation policy).
   - **Messages are authored** and support simple templating (`{{var | round(1)}}`).

3. **Assertions are registry-driven:**

   - Each `ValidationType` ships a registry describing which assertion types, signal templates, and derivations it supports. Registries can declare whether entries are **author-defined**, **provider-defined**, or a mix. For Modelica we expect mostly author-defined entries; EnergyPlus seeds a rich catalog of provider-defined outputs while still allowing authors to add bespoke ones.
   - Assertions capture metadata such as `assertion_type`, `severity`, `target` (e.g., metric path), optional `when` clauses, and a JSON `definition` validated against the registry schema. This definition becomes the payload for CEL evaluation (including optional CEL expressions, thresholds, and templated messages).

4. **Provider Contract** per engine:

   - Supplies a **JSON schema** fragment (what’s allowed for that engine).
   - Publishes a **signal catalog** describing built-in inputs/outputs and whether authors may extend each list.
   - Exposes **CEL helper functions** (e.g., `series(id)`, `p(values,q)`, `mean`, `has`, …).
   - Performs **preflight validation** (e.g., EnergyPlus variable/meter names; Modelica variable paths).
   - Optional **instrument()** to patch a _copy_ of inputs (e.g., add `Output:*` in E+).
   - Builds per-run **bindings** for CEL evaluation.

5. **Prepare (not compile to native):**

   - On ruleset publish/attach, we **validate** shape (JSON Schema), **pre-check** CEL (parse + name/type sanity), collect **dependencies** and **topological order**.
   - We cache the prepared plan for speed.

6. **Findings** remain normalized (severity, code, message, path, meta) and attach to the **ValidationStepRun**.

## Forces / Constraints

- **Safety:** Authors cannot run arbitrary Python—only CEL with our whitelisted helpers.
- **Reproducibility:** Every run must record the exact validator version, resolved ruleset snapshot, and artifacts.
- **Performance:** Many runs may reuse the same ruleset/validator; avoid reparsing every expression.
- **DX:** Rulesets should be readable and portable across projects/domains.

## Detailed Design

### A) Ruleset (author-facing, minimal shape)

```yaml
version: "1.0"
kind: "ruleset"
engine: "ENERGYPLUS" # must match the step's Validator.validation_type
provider:
  instrumentation_policy: "auto_instrument" # or "require_present"

signals:
  inputs:
    - id: floor_area_m2
      from: submission.metadata.floor_area_m2
      type: number
      required: true
  outputs:
    - id: fac_elec_demand_W
      from: provider.series_output("Facility:Electricity:Demand [W]")
      type: timeseries
      required: true

derivations:
  - id: p95_W
    expr: "p(series('fac_elec_demand_W'), 0.95)"
  - id: intensity_Wm2
    expr: "p95_W / floor_area_m2"

assertions:
  - id: p95_cap
    type: threshold.max
    target: "p95_W"
    when: "has(series('fac_elec_demand_W'))"
    severity: "error"
    definition:
      max_value: 1.6e6
    message: "95th percentile {{p95_W | round(0)}} W exceeds 1.6 MW."
  - id: between_integers
    type: cel.expression
    definition:
      expr: "is_int(my_value) && my_value >= 10 && my_value <= 20"
    message: "Value {{my_value}} must be an integer between 10 and 20."
```

- **signals**: author- or provider-defined values; `inputs` are available before execution, `outputs` after provider instrumentation/execution.
- **derivations**: CEL expressions computed from signals (and prior derivations).
- **assertions**: schema-backed configurations that evaluate to booleans. Failures create Findings with the authored message.

### B1) RulesetAssertion model and registry metadata

- **Persistence:** Each Ruleset owns zero or more `RulesetAssertion` rows. Fields include `order`, `assertion_type`, `target`, `when`, `severity`, and a JSON `definition`. The combination of `(ruleset_id, assertion_type, target, order)` keeps the execution stable across edits.
- **Registry contract:** Every `ValidationType` exposes a registry that lists the assertion types it supports, the schema for each type, and how they relate to signals/derivations. Registry entries flag whether signals or derivations are fixed (provider-defined catalog) or must be authored per ruleset. UI editors can introspect this metadata to present dropdowns for EnergyPlus outputs while showing free-form builders for Modelica.
- **Signal catalog split:** Providers can contribute `inputs` (e.g., submission metadata, baseline metrics) and `outputs` (simulation artefacts) separately. Authors can add additional entries to either list where permitted by the registry. During preparation the provider validates that required provider-defined outputs remain intact and that author-defined outputs map to known artefacts.
- **Execution binding:** At runtime the prepared plan merges the registry metadata with the persisted assertions. Providers attach the concrete data sources for each input/output, derivations evaluate in topological order, and assertion handlers render CEL or helper logic using the stored `definition` payload.

### B) Provider Registry (code only)

- In-process registry mapping `(validation_type, version range)` → provider class.
- Selection uses semantic version ranges; **no dotted paths in DB**.

```python
# providers/registry.py (sketch)
from packaging.version import Version

_REGISTRY = {
  "energyplus": [
    ("23.0", "24.0", EPlusProvider_23),
    ("24.0", "25.0", EPlusProvider_24),
  ],
  "modelica": [
    ("1.20", "2.0", ModelicaProvider_1x),
  ],
}

def get_provider(engine_key: str, version: str):
    v = Version(version or "0")
    for lo, hi, cls in _REGISTRY.get(engine_key, []):
        if Version(lo) <= v < Version(hi):
            return cls()
    raise ValueError(f"No provider for {engine_key} {version}")
```

**BaseValidatorEngine** exposes `resolve_provider()` and passes it through the run.

### C) Provider Contract (per engine)

**Must implement:**

- `json_schema()` → provider-specific validation for `provider` block (and optional constraints on signals).
- `signal_catalog()` → returns provider-defined inputs/outputs and whether authors may extend them. Catalog entries can declare availability (pre-run vs post-run) and link to helper functions (e.g., `series_output`).
- `cel_functions()` → dict of safe helpers registered with CEL (`series`, `p`, `max`, `mean`, `sum`, `has`, `is_int`, …).
- `preflight_validate(ruleset)` → domain checks (e.g., E+ names/frequencies exist for that version).
- `instrument(model_copy, ruleset)` → optional patch (E+ `Output:*` on a copy).
- `bind(run_ctx)` → per-run values & closures (e.g., `series()` backed by `eplusout.sql`).

**EnergyPlus notes**

- `instrumentation_policy`:
  - `require_present` → do **not** patch; missing sources create Findings.
  - `auto_instrument` → patch a **copy** of IDF/epJSON to add `Output:*` + `Output:SQLite`.
- `series(id)` lazy-loads from `eplusout.sql` using `provider.outputs` mapping (internal).
- `signal_catalog()` publishes standard meters/variables as outputs so authors can select them without typing raw names; authors may still define custom outputs for bespoke reporting tables.

**Modelica notes**

- `series('System.Room1.Tair')` reads from result files (MAT/CSV/HDF5) or FMU outputs.
- Provide helpers like `final_value(path)` or `steady_state(path, tol, min_duration)` as needed.
- `signal_catalog()` is mostly empty by default; authors enumerate outputs relevant to their custom model. Providers validate that these names exist in the result manifest before execution.

### D) Prepare step (what we store / cache)

On ruleset publish/attach:

- JSON Schema pass (core + provider).
- CEL **pre-check** each derivation `expr`, assertion CEL payload (either `definition.expr` or generated comparisons), and any `when` clauses with the provider’s function signatures; record:
  - normalized expression strings,
  - referenced names (dependencies),
  - expected result kinds (bool/number/list),
  - **topological order** for derivations.

**Cache key:**  
`celplan:{ruleset_sha}:{validator.validation_type}:{validator.version}:{provider.registry_version}`

We keep the source ruleset in the DB; the cache stores the prepared plan so we don’t reparse on every run.

### E) Execution flow (per ValidationStepRun)

1. Engine resolves **Provider** by `(validation_type, validator.version)`.
2. Provider **preflight** (fast fail on broken names).
3. If allowed: **instrument** a copy and run the sim / load artifacts.
4. Provider **bind()** builds CEL bindings (e.g., `series` closure + scalars).
5. Evaluate **derivations** (topo order), then **assertions** (respect `when`).
6. Create **Findings** on failures (message templating with values), persist artifacts and resolved snapshot.

### F) Errors, limits, security

- **At publish/attach:** block rulesets that fail schema or CEL pre-check; return precise errors (path + message).
- **At runtime:** missing series under `require_present` → clean Finding (not crash).
- **Limits:** cap expression length/node count; cap timeseries length; stream loads where possible.
- **Security:** CEL env is closed; only whitelisted helpers. No file/network access from CEL.

## Alternatives Considered

- **Great Expectations / Deequ / Soda** for checks/assertions: powerful for data pipelines, but too heavy and not domain-aware for simulation timeseries.
- **Store dotted provider class paths in DB:** brittle and unsafe across refactors.
- **Put provider version on Ruleset:** creates split-brain vs the chosen Validator; rejected.

## Consequences

**Pros**

- Clear boundaries; one source of truth for version selection.
- Domain safety and author ergonomics (CEL + small helpers).
- Reusable across engines; only Providers differ.
- Fast evaluation with cached plans.

**Cons / Risks**

- Need provider registries + tests for version matching.
- Must maintain domain catalogs (e.g., E+ variable/meter lists per version) for preflight.

## Migration Plan

1. Add `resolve_provider()` to `BaseValidatorEngine`.
2. Implement `providers/base.py` and `providers/registry.py`.
3. Create **EnergyPlusProvider_23** with: schema, preflight (names/frequencies), `series`, `p`, `max/mean/sum`, instrumentation.
4. Add **ruleset prepare** service (schema + CEL pre-check + cache).
5. Update step attach flow to validate rulesets via provider schema + preflight.
6. Update execution path to use prepared plan and provider bindings; persist resolved snapshot and artifacts.
7. Unit/integration tests (publish, run, reproduce).

## Open Questions

- Do we want **two-pass discovery** for E+ (broad `Output:*` once, then re-run narrower)?
- What minimal **templating filters** for messages do we ship at v1 (`round`, `fmt`, `unit`)?
- Do we allow **wildcard keys** in E+ sources (`key: "*"`) and aggregate rules per key vs all-keys?

## Appendix A — CEL helper definitions (behavior)

- `p(values, q)`: q-quantile with linear interpolation; ignores null/NaN; returns null for empty input.
- `series(id)`: provider-backed list of numbers (lazy; cached per run).
- `max/min/mean/sum`: numeric reductions on lists.
- `has(x)`: true if x is non-null / present.
- `is_int(x)`: true if x is an integer (for strict range checks).

## Appendix B — Tiny Python sketches

```python
# Base engine hook
class BaseValidatorEngine:
    validation_type: str
    version: str
    def resolve_provider(self):
        from simplevalidations.providers.registry import get_provider
        return get_provider(self.validation_type, self.version)
```

```python
# Evaluate prepared plan (simplified)
def evaluate_ruleset(prepared, provider, run_ctx):
    bindings = provider.bind(run_ctx)
    values = dict(bindings)

    for d in prepared.derivations_topo:
        values[d.id] = eval_cel(d.program, values)

    findings = []
    for c in prepared.assertions:
        if c.when and not eval_cel(c.when, values):
            continue
        ok = eval_cel(c.assert_prog, values)
        if not ok:
            msg = render_template(c.message_template, values)
            findings.append({"severity": c.severity, "code": c.id, "message": msg})
    return values, findings
```
