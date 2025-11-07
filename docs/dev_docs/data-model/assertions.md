# Ruleset Assertions

Ruleset Assertions capture the checks that a validator will execute once a workflow step
reaches the validation engine. They live in the `ruleset_assertions` table and are referenced by a
ruleset revision so authors can version and reuse collections of checks without mutating workflow
steps directly.

Each assertion row stores:

- `assertion_type` – coarse mode (`basic` vs. `cel_expr`).
- `operator` – normalized comparison operator (only meaningful for `basic` assertions).
- `target_catalog` / `target_field` – FK to a catalog entry or a JSON-style path when the
  validator allows free-form bindings.
- `severity` – maps to the normalized Finding severity (`error`, `warning`, `info`).
- `when_expression` – optional CEL guard that determines whether the assertion runs.
- `rhs` – operator payload (single value, min/max bounds, regex, etc.).
- `options` – operator metadata (inclusive bounds, case folding, tolerance units, etc.).
- `cel_cache` – read-only CEL preview rendered from the operator payload for auditability.
- `message_template` – templated string rendered with evaluation context (e.g., `{{value | round(1)}}`).

Basic assertions reference catalog entries whenever possible so the engine can resolve bindings and
units. When a validator opts into custom targets, a JSON-style path (dot notation + `[index]`) is
persisted in `target_field`. CEL assertions store the raw expression in `rhs["expr"]` and reuse the
`target_*` columns for consistency. BASIC validators always run in “custom target” mode because there
is no provider catalog; authors add assertions manually and the system persists exactly what they enter.

## Relationship to validators and rulesets

1. The author selects a validator (system or custom) while editing the workflow step.
2. The ruleset tied to that step references catalog slugs owned by the validator.
3. Assertions inherit the validator’s helper allowlist, catalog metadata, and provider behavior.

Validators cannot be deleted while rulesets reference them. Catalog edits (for example renaming a
signal) require updating the validator, which in turn triggers validation for every ruleset so slugs
stay synchronized.

## CEL helpers and allowlists

Assertions are evaluated with CEL (Common Expression Language). The preparation service enforces a
two-tier helper allowlist:

1. **Default helpers** from `BaseValidatorEngine` (`has`, `mean`, `percentile`, `duration`, etc.).
2. **Provider helpers** returned by `provider.cel_functions()`, scoped to a `(validation_type, version)` range.

During ruleset publish/attach the service parses every CEL expression (derivations, `when` guards, and
custom assertion payloads) to ensure they reference only allowlisted helpers. The parser also records
dependencies between derivations so assertions evaluate after their inputs exist.

## Preparation workflow

Publishing or attaching a ruleset executes the following steps:

1. Validate the base schema (common fields + provider-specific schema when present).
2. Load and merge validator catalog entries (system + custom) and verify every referenced slug exists.
3. Parse CEL expressions, capturing normalized source and helper usage for auditing.
4. Cache the prepared plan keyed by the ruleset SHA, validator type/version, and provider registry version.

Validation errors surface field-level messages so authors know which assertion failed and why (missing
slug, helper not allowed, expression parse error, etc.).

## Execution flow

Once a validation run reaches a step with assertions:

1. The engine resolves the provider and loads the validator catalog snapshot stored during preparation.
2. Providers optionally `instrument()` the uploaded artifact (EnergyPlus outputs, Modelica probes, etc.).
3. `bind()` constructs helper closures (e.g., `series('p95_W')`) and caches.
4. Derivations execute in topological order, then assertions evaluate sequentially, honoring `when` guards.
5. Failures become Findings with severity, message, target slug, and a copy of the assertion metadata.

This flow ensures findings remain reproducible: rerunning the same submission with the same ruleset +
validator version yields identical catalogs, helper allowlists, and assertion logic.
