# Ruleset Assertions

All assertions in Validibot are `RulesetAssertion` rows. The same model serves
two roles depending on which ruleset the assertion belongs to:

- **Default assertions** live on a validator's `default_ruleset`. They are
  authored by the validator creator and run automatically whenever that validator
  executes in any workflow step. Think of them as built-in checks the validator
  always performs.
- **Step assertions** live on a step-level ruleset attached to a workflow step.
  They are authored by the workflow creator and are specific to one step.

There is no separate model for "validator rules" — the two tiers are expressed
entirely through which `Ruleset` the assertion belongs to.

## Two-tier evaluation

When the validator evaluates assertions for a step, it merges both tiers into a
single pass:

1. Assertions from the validator's `default_ruleset` run first (ordered by
   `order`, then `pk`).
2. Assertions from the step-level ruleset run second (same ordering).

Both tiers produce findings with the same severity model, and both contribute
to the step's assertion statistics (total evaluated, failures).

## Assertion fields

Each `RulesetAssertion` row stores:

- `assertion_type` — coarse mode (`basic` vs. `cel_expr`).
- `operator` — normalized comparison operator (only meaningful for `basic` assertions).
- `target_catalog_entry` / `target_data_path` — FK to a catalog entry or a JSON-style path when the
  validator allows free-form bindings.
- `severity` — maps to the normalized Finding severity (`error`, `warning`, `info`).
- `when_expression` — optional CEL guard that determines whether the assertion runs.
- `rhs` — operator payload (single value, min/max bounds, regex, etc.).
- `options` — operator metadata (inclusive bounds, case folding, tolerance units, etc.).
- `cel_cache` — read-only CEL preview rendered from the operator payload for auditability.
- `message_template` — templated string rendered when the assertion **fails** (e.g., `{{value | round(1)}}`). Supported filters today are:
  - `round(digits)` — rounds numeric values (defaults to `0` digits).
  - `upper` / `lower` — coercion to uppercase or lowercase.
  - `default("fallback")` — substitute the provided fallback when the value is blank/`None`.
- `success_message` — optional message displayed when the assertion **passes**. When set, a SUCCESS severity finding is created for passed assertions. Useful for providing positive feedback to users.

### Assertion targeting

Every assertion targets data in one of two ways — never both, enforced by the
`ck_ruleset_assertion_target_oneof` database constraint:

1. **Declared signal** (`target_catalog_entry` FK) — references a
   `ValidatorCatalogEntry` by its slug. The validator author has pre-declared
   this data point with a name, type, and stage. This is the structured path
   that provides dropdowns, type-appropriate operators, and compile-time
   validation.

2. **Custom data path** (`target_data_path` string) — a free-form
   dot-notation path like `building.thermostat.setpoint` or
   `payload.results[0].value`. Used when the validator doesn't declare signals
   or when the author needs to reference data beyond the declared contract.

Which mode is available depends on the validator's `allow_custom_assertion_targets`
flag. See [Signals — Signals vs custom data paths](signals.md#signals-vs-custom-data-paths)
for the full conceptual explanation.

BASIC validators always use custom data paths because they have no provider
catalog. CEL assertions store the raw expression in `rhs["expr"]` and reuse
the `target_*` columns for consistency.

## Default assertions

Every `Validator` has a `default_ruleset` FK that is auto-populated on save via
`ensure_default_ruleset()`. Validator authors manage default assertions through
the validator detail page, which provides a simplified CEL-only form
(`ValidatorRuleForm`). Under the hood these are regular `RulesetAssertion` rows
on the validator's `default_ruleset`.

Default assertions are always evaluated -- the validator merges them with any
step-level assertions before running the evaluation loop. This means validator
authors can encode domain knowledge (e.g., "site EUI must be positive") that
workflow authors cannot accidentally skip.

## Relationship to validators and rulesets

1. The author selects a validator (system or custom) while editing the workflow step.
2. The validator's `default_ruleset` provides base assertions that always run.
3. The step-level ruleset (optional) adds per-step assertions authored by the
   workflow creator.
4. Assertions inherit the validator's helper allowlist, catalog metadata, and provider behavior.

Validators cannot be deleted while rulesets reference them. Catalog edits (for example renaming a
signal) require updating the validator, which in turn triggers validation for every ruleset so slugs
stay synchronized.

## CEL spec compliance

Assertions are evaluated with [Common Expression Language (CEL)](https://github.com/google/cel-spec)
via the `cel-python` library. We follow the Google CEL spec — in particular, **dot-notation field
selection on maps is supported** because context values are converted to CEL native types
(`celpy.json_to_cel()`) before evaluation. This matches the behavior of Google's reference
implementation (`cel-go`).

For XML data, element attributes are stored with an `@` prefix (e.g., `@Conductivity`). Because
`@` is not valid in CEL identifiers, bracket notation is required: `m["@Conductivity"]` rather
than `m.@Conductivity`. The CEL evaluator detects common mistakes (dot-notation with `@`, missing
`@` prefix) and returns actionable error messages guiding users to the correct syntax.

## CEL helpers and allowlists

Assertions are evaluated with CEL (Common Expression Language). The preparation service enforces a
two-tier helper allowlist:

1. **Default helpers** from `BaseValidator` (`has`, `mean`, `percentile`, `duration`, etc.).
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

1. The validator resolves the provider and loads the validator catalog snapshot stored during preparation.
2. Providers optionally `instrument()` the uploaded artifact (EnergyPlus outputs, Modelica probes, etc.).
3. `bind()` constructs helper closures (e.g., `series('p95_W')`) and caches.
4. Derivations execute in topological order for the **input** stage, then staged assertions run: input-stage assertions gate whether the validator can run, the validator executes, and finally output-stage derivations/assertions consume the emitted telemetry. All assertions continue to honor `when` guards.
5. Failures become Findings with severity, message, target slug, and a copy of the assertion metadata.

This flow ensures findings remain reproducible: rerunning the same submission with the same ruleset +
validator version yields identical catalogs, helper allowlists, and assertion logic.

## Success messages

By default, passed assertions are silent—they don't generate any findings. However, Validibot
supports positive feedback through success messages, which create SUCCESS severity findings when
assertions pass.

There are two ways to enable success messages:

1. **Per-assertion custom message**: Set the `success_message` field on an individual assertion.
   When that assertion passes, a SUCCESS finding is created with the custom message.

2. **Step-level toggle**: Enable `show_success_messages` on a WorkflowStep. When this is true,
   all passed assertions in that step emit success findings. If an assertion has no custom
   `success_message`, a default message is generated (e.g., "Assertion passed: site_eui < 100").

Success messages are useful for:

- Providing positive reinforcement to users submitting data
- Documenting which checks passed for audit trails
- Giving users confidence their data meets requirements

The SUCCESS severity level is distinct from INFO—it specifically indicates a passed check rather
than informational output from the validator.

### Async validators (EnergyPlus, FMU)

Success messages work with async validators just like sync validators. When an EnergyPlus or FMU
Cloud Run Job completes and returns its output envelope, the callback service evaluates any
output-stage assertions against the envelope outputs. If assertions pass and success messages
are configured (via `success_message` or `show_success_messages`), SUCCESS findings are created
alongside any simulation-generated findings.

This means users get the same positive feedback experience regardless of whether their workflow
uses sync validators (Basic, JSON, XML, AI) or async validators (EnergyPlus, FMU).
