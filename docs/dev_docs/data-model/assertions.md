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
- `target_signal_definition` / `target_data_path` — FK to a signal definition or a JSON-style path when the
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

Every assertion target uses a namespace prefix to identify what data it checks.
The form's "Target Path" field accepts these prefixes:

| Prefix | Alias | Stage | Meaning | Example |
|--------|-------|-------|---------|---------|
| `s.` | `signal.` | Input | Workflow signal value | `s.panel_area` |
| `p.` | `payload.` | Input | Raw submission data | `p.building.floor_area` |
| `o.` | `output.` | Output | Validator output | `o.site_eui_kwh_m2` |

The `s.` and `p.` prefixes are always accepted. The `o.` prefix resolves
against the validator's declared output `SignalDefinition` rows. Bare names
(without a prefix) are only accepted when the validator's
`allow_custom_assertion_targets` flag is enabled.

Under the hood, targets are stored in one of two ways — never both, enforced
by the `ck_ruleset_assertion_target_oneof` database constraint:

1. **Declared signal** (`target_signal_definition` FK) — used when the target
   resolves to a known `SignalDefinition` (currently only `o.<name>` targets).
   Provides type-appropriate operators and compile-time validation.

2. **Data path** (`target_data_path` string) — used for `s.<name>`, `p.<path>`,
   and custom bare-name targets. The full prefixed value is stored (e.g.,
   `s.panel_area` or `p.building.thermostat.setpoint`).

The `resolved_run_stage` property on `RulesetAssertion` determines whether an
assertion fires at the input stage (before the validator runs) or the output
stage (after). Targets with `s.` or `p.` prefixes are input-stage; `o.` targets
and bare names are output-stage.

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
authors can encode domain knowledge (e.g., `o.site_eui_kwh_m2 > 0`) that
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

### CEL namespace convention

All CEL expressions use explicit namespaces to avoid ambiguity. The four top-level
namespaces (each with a short alias) are:

| Namespace | Alias | Contents | Example |
|-----------|-------|----------|---------|
| `payload` | `p` | Raw submission or output data | `p.building.floor_area` |
| `signal` | `s` | Workflow signals + promoted outputs | `s.target_eui` |
| `output` | `o` | This step's validator outputs | `o.site_eui_kwh_m2` |
| `steps` | — | Cross-step outputs | `steps.step_a.output.value` |

Raw payload keys are never promoted to bare top-level CEL variables. Authors
access raw data via `p.key`, signals via `s.name`, and outputs via `o.name`.
See [Signals — The CEL context structure](signals.md#the-cel-context-structure) for
full details.

For XML data, element attributes are stored with an `@` prefix (e.g., `@Conductivity`). Because
`@` is not valid in CEL identifiers, bracket notation is required: `p.m["@Conductivity"]` rather
than `p.m.@Conductivity`. The CEL evaluator detects common mistakes (dot-notation with `@`, missing
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
   `success_message`, a default message is generated (e.g., "Assertion passed: o.site_eui_kwh_m2 < 100").

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
