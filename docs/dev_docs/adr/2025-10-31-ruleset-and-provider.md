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

2. **Ruleset format (v1) is engine-agnostic and adapts to the validator class:**

   - **Schema-style validators** (JSON Schema, XML Schema, etc.) keep the ruleset payload small: the stored artifact _is_ the schema plus a bit of metadata, because every constraint in those formats already acts like an assertion. These rulesets do **not** create `RulesetAssertion` rows.
   - **Assertion-style validators** (EnergyPlus, Modelica, WinCalc, …) keep all signal/derivation catalogs on the **Validator**. Rulesets only persist `RulesetAssertion` rows that reference those catalog slugs. This keeps system-defined definitions centralized while still letting authors reuse them across workflows.
   - `signals` are split into **inputs** (available before execution) and **outputs** (materialised post-run). Providers seed the catalog for stock validators, while custom validators let authors define additional entries once per validator.
   - A small `provider` block remains for non-version options (e.g., instrumentation policy).
   - **Messages are authored** and support simple templating (`{{var | round(1)}}`).

3. **Validators own the catalogs and helper allowlists:**

   - Each `ValidationType` (and its specific Validator rows) ships a registry describing (a) allowed assertion types, (b) the CEL helper allowlist, and (c) the catalog of admissible signals/derivations. Built-in validators (EnergyPlus v23, XML Schema, etc.) seed these definitions in code/migrations; custom validators let authors create their own catalogs once and reuse them wherever that validator is referenced.
   - Rulesets can only reference the catalog entries defined for their validator. Changing a validator (or editing a custom validator) updates the catalog for every ruleset that depends on it.
   - Assertions capture metadata such as `assertion_type`, `severity`, `target` (e.g., metric path), optional `when` clauses, and a JSON `definition` validated against the registry schema. This definition becomes the payload for CEL evaluation (including optional CEL expressions, thresholds, and templated messages).

4. **Provider Contract** per engine:

   - Supplies a **JSON schema** fragment (what’s allowed for that engine).
   - Publishes the **validator catalog** describing built-in inputs/outputs/derivations for that validation type (and whether custom validators may extend it).
   - Exposes **CEL helper functions** (e.g., `series(id)`, `percentile(values,q)`, `mean`, `has`, …).
   - Performs **preflight validation** (e.g., EnergyPlus variable/meter names; Modelica variable paths).
   - Optional **instrument()** to patch a _copy_ of inputs (e.g., add `Output:*` in E+).
   - Builds per-run **bindings** for CEL evaluation.

5. **Prepare (not compile to native):**

   - On ruleset publish/attach, we **validate** shape (JSON Schema), **pre-check** CEL (parse + name/type sanity), collect **dependencies** and **topological order**.
   - We cache the prepared plan for speed.

6. **Findings** remain normalized (severity, code, message, path, meta) and attach to the **ValidationStepRun**.

### Terminology: Provider vs Validator vs Ruleset

- **Validator** — the database row selected by a workflow step. It pairs a `validation_type` (e.g., `ENERGYPLUS`) with a semantic version (e.g., `23.2`) or with a custom validator configuration (e.g., `CustomValidatorType.MODELICA`). Validators own the authoritative catalog of signals/derivations and the CEL helper allowlist for their domain.
- **Provider** — the runtime implementation (Python class) resolved by the engine for a given validator/version. A provider knows how to instrument inputs, load artifacts, hydrate catalog entries, and execute CEL expressions. Think of it as the “adapter” between the validator definition and actual simulation files.
- **Ruleset** — the author-owned bundle of assertions (and, for schema validators, the schema artifact) referenced by workflow steps. Rulesets never redefine provider catalogs; they only reference the validator’s entries.

## Forces / Constraints

- **Safety:** Authors cannot run arbitrary Python—only CEL with our whitelisted helpers.
- **Reproducibility:** Every run must record the exact validator version, resolved ruleset snapshot, and artifacts.
- **Performance:** Many runs may reuse the same ruleset/validator; avoid reparsing every expression.
- **DX:** Rulesets should be readable and portable across projects/domains.

## Detailed Design

### A) Ruleset shapes

We support two complementary shapes so that simple schema validators stay lightweight while simulation-heavy validators embrace Assertions.

1. **Schema-style validators (JSON / XML / CSV layout):**

   - The Ruleset stores metadata (`name`, `slug`, `version`, `validation_type`) plus the raw schema blob (`json_schema`, `xsd`, etc.) and optional author notes.
   - No `RulesetAssertion` rows are created. Findings are emitted directly by the schema engine, reusing existing behavior.
   - Example payload:

     ```json
     {
       "validation_type": "JSON",
       "schema_version": "draft2020-12",
       "artifact_path": "rulesets/rs_123/schema.json",
       "checksum": "sha256:abc123",
       "metadata": {
         "description": "Submission payload v3",
         "owner_project": "api-clients"
       }
     }
     ```

2. **Assertion-style validators (EnergyPlus, Modelica, WinCalc, …):**

   - The Ruleset stores high-level metadata plus `RulesetAssertion` rows. The signals/derivations it can reference live on the associated Validator (`validator_catalog_entries`).
   - Authors manage catalog entries through the validator editor (for custom validators) or consume the stock catalog (for built-in validators). Assertions reference catalog slugs but never redefine them.
   - Persisted rows:
     - `ValidatorCatalogEntry(id, validator_id, entry_type, slug, data_type, availability, binding_config)`
     - `RulesetAssertion(id, ruleset_id, assertion_type, target_slug, severity, when_expr, definition_json, order, message_template)`
   - Provider options live in `Ruleset.provider_config` (JSON) for instrumentation policy, runtime toggles, etc.
   - Conceptual example (rendered for discussion, actual persistence is relational):

     ```yaml
     provider:
       instrumentation_policy: auto_instrument
     catalogs:
       system_inputs:
         - slug: floor_area_m2
           data_type: number
           binding: submission.metadata["floor_area_m2"]
       author_outputs:
         - slug: fac_elec_demand_W
           data_type: timeseries
           binding: provider.series_output("Facility:Electricity:Demand [W]")
     derivations:
       - slug: p95_W
         expr: "percentile(series('fac_elec_demand_W'), 0.95)"
       - slug: intensity_Wm2
         expr: "p95_W / floor_area_m2"
     assertions:
       - slug: p95_cap
         type: threshold.max
         target: p95_W
         severity: error
         definition:
           max_value: 1.6e6
         message: "95th percentile {{p95_W | round(0)}} W exceeds 1.6 MW."
     ```

### B) Validator-owned catalogs and custom validators

Catalog definitions now live beside the Validator instead of being embedded in every ruleset. We introduce two supporting models:

| Table                       | Purpose                                                                   | Key Columns                                                                                                                                      |
| --------------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `validator_catalog_entries` | Canonical catalog entries for a specific validator row (stock or custom). | `validator_id`, `entry_type` (`signal_input`, `signal_output`, `derivation`), `slug`, `data_type`, `availability`, `binding_config`, `metadata`. |
| `custom_validators`         | Author-configurable validators layered on top of a base `ValidationType`. | `id`, `owner_org`, `base_validation_type`, `custom_type` (`MODELICA`, `PYWINCALC`, …), `name`, `slug`, `description`.                            |

Stock validators (EnergyPlus v23, JSON Schema v1, etc.) seed `validator_catalog_entries` via migrations/tests. Custom validators are created through a new UI surface:

1. Author chooses a base validation type and a `CustomValidatorType` (initially `MODELICA` or `PYWINCALC`).
2. Author defines signals (inputs/outputs), derivations, and optional helper metadata once for that validator.
3. Saving the custom validator persists a `Validator` row plus its catalog entries. Any ruleset that references this validator now sees the author-defined catalog.

`RulesetAssertion` rows reference catalog slugs for their `target` and for derived inputs inside `definition_json`. During execution the engine loads the validator’s catalog entries and hydrates those slugs into concrete bindings. Changing a catalog entry (for example renaming a Modelica signal) requires editing the validator; once saved, every ruleset using that validator observes the change.

### C) Provider registry (code only)

Regarding Providers: Think of the Provider as the engine’s adapter. It bundles every domain-specific helper the validator
needs when a step runs. That usually means:

- defining the signal/derivation catalog for the validator version (or reading the catalog if it’s a custom validator);
- exposing the CEL helper functions the engine allows;
- performing preflight checks on the artifacts/config;
- optionally instrumenting inputs (e.g., adding EnergyPlus output objects)

How do we locate a provider class? We use a registry, similar to how we find engines.

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

`BaseValidatorEngine.resolve_provider()` is a thin wrapper that calls into this registry and caches the class instance for the run.

### D) Provider contract (per validation type)

Providers must implement:

- `json_schema()` → provider-specific validation for the optional `provider_config` block.
- `catalog_entries(validator)` → returns the catalog rows that should exist for a validator (built-in validators read from code/migrations, custom validators read the DB-backed rows the author created). The preparation service compares this list against `validator_catalog_entries` to ensure nothing drifted.
- `cel_functions()` → dict of safe helper descriptors (`name`, signature, return type, docstring, implementation). Providers may return only their custom helpers; the runtime adds the default helpers automatically (see Section E).
- `preflight_validate(ruleset, merged_catalog)` → domain checks (EnergyPlus variable/meter names, Modelica variable paths, etc.).
- `instrument(model_copy, ruleset)` → optional patch (e.g., add `Output:*` objects for EnergyPlus when auto instrumentation is enabled).
- `bind(run_ctx, merged_catalog)` → per-run CEL bindings (closures for `series()`, scalar metadata, derived caches).

**EnergyPlus specifics**

- Supports `instrumentation_policy = require_present | auto_instrument`.
- Stock validators seed the measurement catalog with the DOE-standard list of meters/variables for each engine version; to extend the catalog, create a custom EnergyPlus validator (which duplicates the defaults and allows edits) rather than modifying individual rulesets.
- Helper additions: `series(id)`, `kw_to_w(value)`, `delta_T(series_a, series_b, unit='C')`.

**Modelica specifics**

- The base Modelica provider ships a minimal catalog; authors are expected to define their own signals/derivations when they create a custom validator of type `MODELICA`. Those catalog rows live with the validator and are reused by every ruleset referencing it.
- Helper additions: `series(path)`, `final_value(path)`, `steady_state(path, tol, min_duration)`.

### E) CEL helper registry and allowlists

CEL evaluation is locked down by a two-tier registry:

1. **Default helper set** (applied to all validation types via `BaseValidatorEngine`): `has(x)`, `is_int(x)`, `percentile(values, q)`, `mean(values)`, `sum(values)`, `max(values)`, `min(values)`, `abs(value)`, `round(value, digits)`, `duration(series, predicate)`.
2. **Provider helpers**: each validation type may append helpers via `cel_functions()`. The provider registry records these helpers with the same semantic version range as the provider class so audits can see which functions were available for a run.

During preparation we parse every CEL expression (derivations, assertions, `when` clauses) and verify that all referenced helper names exist in the merged allowlist. Expressions referencing anything else fail fast with actionable errors. The allowlist metadata is also surfaced to the UI so authors always know which helpers are legal for the validator they picked.

### F) Prepare step (what we store / cache)

On ruleset publish/attach we:

1. Validate the ruleset’s base schema (common fields + provider schema when present).
2. Load the validator’s catalog entries (built-in or custom) and ensure every slug referenced by the ruleset exists.
3. Parse each derivation and assertion CEL snippet with the helper allowlist; collect:
   - normalized expression strings,
   - referenced helper names (for auditing),
   - dependencies/topological order across derivations,
   - expected return kinds (bool, number, list).
4. Cache the resulting “prepared plan” keyed by `celplan:{ruleset_sha}:{validator.validation_type}:{validator.version}:{provider.registry_version}`.

### G) Execution flow (per ValidationStepRun)

1. Engine resolves the Provider by `(validation_type, validator.version)`.
2. Load the validator’s catalog entries and run `preflight_validate`.
3. If allowed, `instrument()` a copy of the uploaded artefact (EnergyPlus IDF, Modelica FMU config, etc.) and run the simulation or parse the submission.
4. Provider `bind()` builds CEL bindings (e.g., `series` closures, scalar metadata).
5. Evaluate derivations in topological order, caching results in the evaluation context.
6. Evaluate assertions, honoring `when` guards. Failures become Findings with templated messages.
7. Persist run artifacts plus the resolved catalog snapshot for reproducibility.

### H) Errors, limits, security

- **At publish/attach:** block rulesets that fail schema validation, catalog ownership rules, helper allowlist checks, or CEL parsing; return precise errors (field path + message).
- **At runtime:** missing signals under `require_present` instrumentation are converted into Findings rather than crashes.
- **Limits:** cap CEL expression length/node count, enforce max timeseries length, stream large artifacts where possible.
- **Security:** CEL environment remains closed; only allowlisted helpers execute and they receive copies/handles scoped to the run (no filesystem or network access).

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

## User Interface

### Validation Library

We will have a new item in the left navigation called "Validation Library"
In this section we need a way to CRUD custom validations. The user can also see system validations in read-only format.

A user with MANAGE_VALIDATION_LIBRARY can create and edit custom validations.

A custom validation can be based on one of the following types MODELICA_MODEL, MATLAB_SCRIPT

A user defined the custom validation's name, slug, description, public description, and active True/False.

A user should be allowed to define the input signals, output signals and derivations allowed for this custom validation.
Each item should have a short_description to be used in the Assertion UI (see below).

### Assertions

When a user adds a workflow step, it may now a two-step process:

For simple validation types like JSON_SCHEMA or XML_SCHEMA, the user creates the step and provides the schema for the Ruleset all in one screen

However, now for advacned validations like ENERGYPLUS or a custom type like MODELICA_MODEL or MATLAB_SCRIPT, the user has an initial screen
to add the Step name, description, author notes. The next step is essentially the Assertion CRUD screen, where users can create, edit and
delete one more more assertions for the ruleset. We need a nice, HTMx-based UI that allow this, as well as a read-only portion of the screen
that shows available input signals, output signals, derivation names (and what they do).

Assertions should work just like the validations steps: The use should be able to drag-and-drop the order of the assertions, and delete or edit them easily.
Since Assertions do not have many fields, the create/edit UI can appear directly in a modal.

## Open Questions

- Do we want **two-pass discovery** for E+ (broad `Output:*` once, then re-run narrower)?
- What minimal **templating filters** for messages do we ship at v1 (`round`, `fmt`, `unit`)?
- Do we allow **wildcard keys** in E+ sources (`key: "*"`) and aggregate rules per key vs all-keys?

## Implementation Plan (2025-11-05)

We are landing the ADR in four deliberate phases so downstream changes stay reviewable:

1. **Models & Services (in progress)**  
   - Add `validator_catalog_entries` and `custom_validators` tables/models.  
   - Update preparation/runtime services so catalogs live on the validator (rulesets reference slugs only).  
   - Introduce CEL helper allowlists at the engine level.

2. **Provider & Engine Updates**  
   - Flesh out the provider registry and hook `BaseValidatorEngine` into it.  
   - Implement the minimal EnergyPlus provider behavior from the ADR (single-pass instrumentation, explicit catalog).  
   - Ensure schema-style validators continue to work with the shared plumbing.

3. **Validation Library UI**  
   - Add a “Validation Library” nav item with read-only system validator pages and CRUD for custom validators (MODELICA, PYWINCALC to start).  
   - Wire permissions (org admins/authors) and surface the validator catalog editor.

4. **Ruleset Assertion UX & Docs**  
   - Update the ruleset/step editors to reference validator catalog slugs, including new assertion tooling.  
   - Refresh developer docs + authoring guides with the validator/provider/ruleset flow.  
   - Add integration tests covering custom validators, catalog edits, and validation runs.

Notes for future phases will be appended here as we encounter edge cases.

### Phase 1 Notes (2025-11-05)

- Added `validator_catalog_entries` + `custom_validators` models/migrations.
- Validators now carry `org`/`is_system` flags and expose catalog helper APIs.
- Base validator engines publish a default CEL helper allowlist; subclasses will extend it in later phases.
- Rulesets continue to resolve their validator context via attached workflow steps (no direct FK stored) to keep the data model normalized.

### Phase 2 Notes (2025-11-05)

- Introduced the provider registry + base provider contract along with the EnergyPlus provider seeding default catalog entries.
- Engine bootstrap now resolves providers so default catalogs stay synced (`create_default_validators` also syncs during bootstrap).
- Providers can extend CEL helpers and instrumentation hooks in later phases without touching workflow code.

## Appendix A — CEL helper definitions (behavior)

- `percentile(values, q)`: q-quantile with linear interpolation; ignores null/NaN; returns null for empty input.
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
