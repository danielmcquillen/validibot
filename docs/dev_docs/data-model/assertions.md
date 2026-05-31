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

Every assertion target uses a namespace prefix to identify what data it
checks. The form's "Target Path" field accepts these prefixes:

| Prefix | Alias | Available at stage | Meaning | Example |
|--------|-------|--------------------|---------|---------|
| `p.` | `payload.` | Input + Output | Raw submission data | `p.building.floor_area` |
| `s.` | `signal.` | Input + Output | Workflow signal (from `WorkflowSignalMapping` or promotion) | `s.target_eui` |
| `i.` | `input.` | Input + Output | Step input (parser facts, resolved bindings) | `i.zone_count` |
| `o.` | `output.` | **Output only** | Step output | `o.site_eui_kwh_m2` |
| `steps.<key>.input.` / `steps.<key>.output.` | — | Input + Output | Earlier step's inputs/outputs | `steps.preflight.output.warning_count` |

**Stage-aware availability.** Step outputs (`o.*`) only exist after the
step's validator runs, so they should not be referenced in input-stage
assertions — at runtime such references silently resolve to null.

**Partial enforcement today.** Per ADR-2026-05-22's reconciliation
notes, ``get_catalog_choices()`` accepts a ``stage`` parameter and
can filter the autocomplete to exclude this step's ``o.*`` entries,
and ``resolved_run_stage`` classifies CEL assertions explicitly
referencing ``i.*`` as input-stage. **Strict edit-time rejection of
``o.*`` references in input-stage assertions is planned but not yet
threaded through every view call site.** The deferred work and its
acceptance criteria are tracked in ADR-2026-05-22.

Bare names (without a prefix) are only accepted when the validator's
`allow_custom_assertion_targets` flag is enabled.

Under the hood, targets are stored in one of two ways — never both,
enforced by the `ck_ruleset_assertion_target_oneof` database constraint:

1. **Declared step input/output definition** (`target_signal_definition`
   FK on the `StepIODefinition` model, renamed from `SignalDefinition`
   per ADR-2026-05-22b — internal)
   — used when the target resolves to a known step input
   (`i.<contract_key>`) or step output (`o.<contract_key>`). Provides
   type-appropriate operators and compile-time validation. The FK name
   is intentionally left at its legacy `target_signal_definition` value
   to keep the database column and migrations stable; only the model
   class was renamed.

2. **Data path** (`target_data_path` string) — used for `s.<name>`,
   `p.<path>`, and custom bare-name targets. The full prefixed value is
   stored (e.g., `s.target_eui` or `p.building.thermostat.setpoint`).

The `resolved_run_stage` property on `RulesetAssertion` determines
whether an assertion fires at the input stage (before the validator
runs) or the output stage (after). Targets with `s.`, `p.`, or `i.`
prefixes can be used at either stage; `o.` targets and output-direction
signal definitions are output-stage only.

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

All CEL expressions use explicit namespaces to avoid ambiguity. The five
top-level namespaces (each with a short alias where applicable) are:

| Namespace | Alias | Contents | Example |
|-----------|-------|----------|---------|
| `payload` | `p` | Raw submission data | `p.building.floor_area` |
| `signal` | `s` | Workflow signals + promoted step inputs/outputs | `s.target_eui` |
| `input` | `i` | This step's step inputs (parser facts, resolved bindings) | `i.zone_count` |
| `output` | `o` | This step's step outputs (after the validator runs) | `o.site_eui_kwh_m2` |
| `steps` | — | Earlier steps' inputs and outputs | `steps.step_a.output.value`, `steps.step_a.input.zone_count` |

Raw payload keys are never promoted to bare top-level CEL variables.
Authors access raw data via `p.key`, workflow signals via `s.name`, step
inputs via `i.name`, and step outputs via `o.name`. See
[Signals — The CEL context structure](signals.md#the-cel-context-structure)
for full details, including which namespaces are populated for which
validator types (the
[process-centric spectrum](signals.md#when-do-step-inputs-and-step-outputs-exist)).

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

### Adding a built-in helper: three registrations

A Validibot-specific helper (one that is not a CEL builtin) is only usable end-to-end when it is
registered in **three** places. Miss one and the helper either can't be authored or fails at run time:

1. **Documentation** — a `CelHelper` entry in `validations/cel.py` `DEFAULT_HELPERS`. Drives the
   editor's help tooltip. The set of names there is also the single source of truth for the
   authoring-time allowlists, exported as `CUSTOM_HELPER_NAMES`.
2. **Authoring-time allowlist** — every CEL identifier check derives its accepted custom-helper names
   from `CUSTOM_HELPER_NAMES`, so a name added to `DEFAULT_HELPERS` is automatically accepted by the
   ruleset form, the rule-builder view, and signal-name resolution. (These allowlists used to hand-list
   the names in four separate places; they now share the one set so they cannot drift.)
3. **Runtime binding** — an executable implementation in `validations/cel_helpers.py`, bound onto the
   compiled program by `validations/cel_eval.py` via `celpy.Environment.program(ast, functions=...)`.
   Without this a helper parses and saves but raises an unknown-function error when evaluated.

`evaluate_cel_expression()` caches the parsed **AST** (the expensive step) and builds a fresh program
per call, so run-specific helpers can be bound without sharing state across runs.

### Built-in helpers and `now()`

The runtime-bound built-in helpers, all implemented in `cel_helpers.py`, are:

- **Date/time:** `is_iso8601(s)`, `parse_date(s)` (ISO 8601 string → `timestamp` or `null`), and `now()`.
  Parsing is locale-independent and fixed-format (ISO 8601 only) so findings are reproducible.
- **Scalar:** `is_finite(n)`, `is_int(n)` (integral check — `2.0` is integral, `2.5` is not), `abs(n)`
  (type-preserving), and `round(n, digits=0)` (round-half-to-even, deterministic).
- **Aggregate (over a list):** `mean`, `sum`, `min`, `max`, and `percentile(values, q)` (q in 0–100,
  linear interpolation). These ignore nulls and **return a double**.

Two practical notes on the aggregates. First, they return a double, and celpy rejects `double == int`
equality (`mean(xs) == 2` errors), so compare against a double literal (`mean(xs) == 2.0`) or use an
ordered comparison (`mean(xs) > 2`). Second, a malformed input (a non-list, or a non-numeric element)
yields `null`, which makes the comparison fail rather than computing over garbage.

Not every documented name is a Validibot binding. `duration("3600s")` is a **CEL built-in** (it parses a
duration string), so it is intentionally *not* bound in `cel_helpers.py` — doing so would shadow the
built-in. `has(...)` is a CEL macro with its own syntax. Both are allowlisted because CEL provides them.

`now()` is **not** a CEL builtin and is **not** the wall clock — CEL has no nondeterministic builtins by
design. It is bound to a pinned instant supplied per evaluation (`evaluate_cel_expression(..., now=...)`,
which callers set to the run's `started_at`). If no clock is supplied, `now()` is left unbound and an
expression that calls it fails cleanly rather than reading the wall clock. This is what makes a
time-relative assertion such as `parse_date(row.eventDate) <= now()` reproducible for a given run.

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
