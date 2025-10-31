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

We chose **CEL** (Common Expression Language) for the ruleset’s expressions, plus **domain Providers** that expose CEL helpers and validate domain config.

## Decision

1. **Validator is the single source of truth for provider version.**

   - The **WorkflowStep** selects a **Validator** row (e.g., `ENERGYPLUS v23.2`).
   - The engine resolves a **Provider** implementation **in code** via a registry keyed by `(validation_type, validator.version)`.
   - **Rulesets do not store provider class names or versions.**

2. **Ruleset format (v1) is engine-agnostic and tiny:**

   - `signals` (named values bound into CEL), `derivations` (CEL expressions), `checks` (CEL booleans with messages), plus a small `provider` block for non-version options (e.g., instrumentation policy).
   - **Messages are authored** and support simple templating (`{{var | round(1)}}`).

3. **Provider Contract** per engine:

   - Supplies a **JSON schema** fragment (what’s allowed for that engine).
   - Exposes **CEL helper functions** (e.g., `series(id)`, `p(values,q)`, `mean`, `has`, …).
   - Performs **preflight validation** (e.g., EnergyPlus variable/meter names; Modelica variable paths).
   - Optional **instrument()** to patch a _copy_ of inputs (e.g., add `Output:*` in E+).
   - Builds per-run **bindings** for CEL evaluation.

4. **Prepare (not compile to native):**

   - On ruleset publish/attach, we **validate** shape (JSON Schema), **pre-check** CEL (parse + name/type sanity), collect **dependencies** and **topological order**.
   - We cache the prepared plan for speed.

5. **Findings** remain normalized (severity, code, message, path, meta) and attach to the **ValidationStepRun**.

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
  - id: floor_area_m2
    from: submission.metadata.floor_area_m2
    type: number
    required: true

derivations:
  - id: p95_W
    expr: "p(series('fac_elec_demand_W'), 0.95)"
  - id: intensity_Wm2
    expr: "p95_W / floor_area_m2"

checks:
  - id: p95_cap
    when: "has(series('fac_elec_demand_W'))"
    severity: "error"
    assert: "p95_W <= 1.6e6"
    message: "95th percentile {{p95_W | round(0)}} W exceeds 1.6 MW."
  - id: between_integers
    assert: "is_int(my_value) && my_value >= 10 && my_value <= 20"
    message: "Value {{my_value}} must be an integer between 10 and 20."
```

- **signals**: any bound values (metadata scalars, parsed tables, provider data handles).
- **derivations**: CEL expressions computed from signals (and prior derivations).
- **checks**: CEL booleans → Findings on `false` with author message.

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
- `cel_functions()` → dict of safe helpers registered with CEL (`series`, `p`, `max`, `mean`, `sum`, `has`, `is_int`, …).
- `preflight_validate(ruleset)` → domain checks (e.g., E+ names/frequencies exist for that version).
- `instrument(model_copy, ruleset)` → optional patch (E+ `Output:*` on a copy).
- `bind(run_ctx)` → per-run values & closures (e.g., `series()` backed by `eplusout.sql`).

**EnergyPlus notes**

- `instrumentation_policy`:
  - `require_present` → do **not** patch; missing sources create Findings.
  - `auto_instrument` → patch a **copy** of IDF/epJSON to add `Output:*` + `Output:SQLite`.
- `series(id)` lazy-loads from `eplusout.sql` using `provider.outputs` mapping (internal).

**Modelica notes**

- `series('System.Room1.Tair')` reads from result files (MAT/CSV/HDF5) or FMU outputs.
- Provide helpers like `final_value(path)` or `steady_state(path, tol, min_duration)` as needed.

### D) Prepare step (what we store / cache)

On ruleset publish/attach:

- JSON Schema pass (core + provider).
- CEL **pre-check** each `expr`/`when`/`assert` with the provider’s function signatures; record:
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
5. Evaluate **derivations** (topo order), then **checks** (respect `when`).
6. Create **Findings** on failures (message templating with values), persist artifacts and resolved snapshot.

### F) Errors, limits, security

- **At publish/attach:** block rulesets that fail schema or CEL pre-check; return precise errors (path + message).
- **At runtime:** missing series under `require_present` → clean Finding (not crash).
- **Limits:** cap expression length/node count; cap timeseries length; stream loads where possible.
- **Security:** CEL env is closed; only whitelisted helpers. No file/network access from CEL.

## Alternatives Considered

- **Great Expectations / Deequ / Soda** for checks: powerful for data pipelines, but too heavy and not domain-aware for simulation timeseries.
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
    for c in prepared.checks:
        if c.when and not eval_cel(c.when, values):
            continue
        ok = eval_cel(c.assert_prog, values)
        if not ok:
            msg = render_template(c.message_template, values)
            findings.append({"severity": c.severity, "code": c.id, "message": msg})
    return values, findings
```
