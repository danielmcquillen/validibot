# ADR-0021: CEL Rulesets, Provider Selection, and Execution Model

**Date:** 2025-10-31  
**Status:** Proposed (seeking acceptance)  
**Owners:** Platform / Validations

## Context

Validibot runs **workflows** composed of **steps**. Each step executes a **Validation** which uses a
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

## Detailed Design Reference

The granular treatment of validators, catalogs, providers, derivations, and assertions now lives in the
developer documentation:

- [Data Model / Validators](../data-model/validators.md)
- [Data Model / Ruleset Assertions](../data-model/assertions.md)

Those pages describe catalog schemas, CEL helper allowlists, the provider contract, and execution flow.
This ADR focuses on the forces behind the decision and the rollout chronology below.

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

### Validator Library

We will have a new item in the left navigation called "Validator Library"
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

3. **Validator Library UI**

   - Add a Validator Library” nav item with read-only system validator pages and CRUD for custom validators (MODELICA, PYWINCALC to start).
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

### Phase 3 Notes (2025-11-05)

- Added the Validator Library UI (left-nav item) so authors can browse system validators and org-specific custom validators.
- Implemented custom-validator CRUD with permission checks (owners/admins/authors) and templates for list/detail/form/delete flows.
- Context processors now expose `can_manage_validators`, and template navigation reflects the new entry.

### Phase 4 Workplan (2025-11-06)

- Ruleset editor must load the validator catalog so assertions reference valid slugs.
- Step editor UX needs to surface assertions inline (catalog pickers, severity, message template).
- Prepare service should hydrate catalog metadata per ruleset and enforce slug existence at publish time.
- Minimal assertion persistence model is required (start with a simple JSON payload tied to ruleset; can expand later).

### Phase 4 Notes (2025-11-06)

- Added the assertion data model (`RulesetAssertion`) plus CRUD UI for advanced workflow steps.
- Workflow step editing now redirects to the Assertions panel for validators like EnergyPlus, and the UI mimics the workflow-step list with modal create/edit forms.
- Validator catalogs drive the assertion forms, ensuring every rule references a known slug; backend tests cover the create/reorder flows.

## Appendix A — CEL helper definitions (behavior)

Current helper allowlist implemented in code:

- `has(value)`: true if value is non-null / present.
- `is_int(value)`: true if value is an integer.
- `percentile(values, q)`: q-quantile with linear interpolation; ignores null/NaN; returns null for empty input.
- `mean(values)`: average of numeric list (ignores nulls).
- `sum(values)`: sum of numeric list.
- `max(values)`: maximum of numeric list.
- `min(values)`: minimum of numeric list.
- `abs(value)`: absolute value of a number.
- `round(value, digits)`: rounds to the provided decimal places.
- `duration(series, predicate)`: count of samples where `predicate(series[i])` is true.

Planned additions (not yet allowlisted): `series(id)` for provider-backed series lookups plus the broader helper set described in ADR-2015-11-16 (string helpers, collection helpers, ceil/floor, casts, etc.).

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
