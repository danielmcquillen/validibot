# ADR-2015-11-16-CEL-Implementation: Fully implement the CEL feature

## Status

Proposed (2025-11-16)

## Background

As part of an Workflow definition, the author may have defined Assertion instances using CEL syntax to perform basic expressions on input signals, output signals and derived signals.

This processing should happen as part of the Validator engine. As every validator instance may have these kinds of expressions, the feature should be implemented in a way that makes it fundamental to the engine code structure.

We need to have this CEL mini-engine within the Validator engine defined in a way that is safe and which supports all the current functionality defined for the different Validator types. It should also be extensible to support new CEL expressions in the future.

However, since SimpleValidations is still an MVP, we can keep things simple for now.

## Decision (draft)

1. **CEL engine integration**
   - Use the Python CEL implementation (e.g., `cel-python`/`cel-go` bindings) embedded in the validation engine. Keep invocation confined to the worker context (never the web process) to avoid request-path blocking and to limit blast radius.
   - Define a single, consistent expression context shape for all validators: a dict of signals keyed by `ValidatorCatalogEntry.slug` (inputs, outputs, derived), plus optional metadata (submission id, org id) if needed for logging only.
   - Keep assertion definitions in `Ruleset` as today; assertions refer to catalog entry slugs. No CEL expressions in `ValidatorCatalogEntry` itself—only in assertions.
   - Engine choice for MVP: **cel-python**
     - Pros: pure Python, no extra service; deploys with existing workers; actively maintained (Cloud Custodian, Netchecks); compile-once/eval-many supported; performance is ample relative to our workloads.
     - Cons: can lag `cel-go` on bleeding-edge features; raw performance lower than `cel-go` but not a bottleneck for our use cases.

2. **Safety and sandboxing**
   - Disable all host-aware functions; only allow pure CEL functions/operators. No filesystem, network, or datetime side-effects (provide deterministic helper functions if needed).
   - Enforce timeouts per assertion evaluation; fail the assertion (and mark the step run errored) on timeout or compilation errors.
   - Limit memory/AST size and depth to prevent resource exhaustion.

3. **Scope for MVP**
   - Static expression evaluation only (no stateful accumulators across time).
   - Support numeric, boolean, string, list/object access, and standard CEL comparison/logical operators.
   - Current helper allowlist implemented in code: `has`, `is_int`, `percentile`, `mean`, `sum`, `max`, `min`, `abs`, `round`, `duration`.
   - Planned (not yet allowlisted) helpers: `series` for provider-backed timeseries plus the broader set originally proposed (len/exists/isEmpty/notEmpty, ceil/floor/clamp, string helpers like contains/startsWith/endsWith/matches, collection helpers such as all/any/map/filter/distinct, and casting helpers like toNumber/toString/toBool). Date/time helpers remain deferred.
   - No user-defined functions/macros in MVP; consider later.

4. **Types and validation**
   - Validate assertion references at authoring time: every identifier in the CEL AST must resolve to a `ValidatorCatalogEntry.slug` (or approved helper).
   - Use catalog entry value_type metadata to cast context values before evaluation; on cast failure, treat as an assertion failure with a simple error message.
   - On run, build the context from the signals present in the step run; if required signals are missing, mark the assertion as failed with a clear reason.

5. **Execution flow**
   - For each step run, after the validator produces outputs, materialize a context: `{slug: value}` for all available signals (inputs, outputs, derived).
   - For each assertion in the step’s Ruleset, compile (cache) and evaluate the CEL expression against the context.
   - Collect findings: on false/exception/timeout, emit a finding with severity/code/path as per the assertion definition; on true, no finding.

6. **Caching and performance**
   - Cache compiled CEL programs per assertion id + version/updated_at to avoid recompiling on every run.
   - Recompile on assertion change; bust cache on Ruleset updates.

7. **Observability**
   - Log assertion errors/timeouts with assertion id, step run id, and short diagnostics; optionally include a redacted snippet of the context keys, not full values, for privacy.

8. **Documentation**
   - Add thorough dev docs covering:
     - The expression context shape and how signals map to `ValidatorCatalogEntry.slug`.
     - The allowed functions/operators, limits (timeout, AST size), and error behaviors.
     - Examples for common checks (range checks, string validation, list ops) and how to reference signals from different run stages.
   - Add user docs with step-by-step examples of writing CEL assertions, including sample expressions for inputs/outputs and guidance on handling missing/optional signals.
   - Treat documentation as a first-class deliverable in each implementation step; follow AGENTS.md guidance, Google Python style, and provide clear code/module comments where needed for developer comprehension.

9. **Timeouts and limits (constants)**
   - Define in `validations/constants.py` (adjust as needed):
     - `CEL_MAX_EVAL_TIMEOUT_MS` (e.g., 100 ms per assertion for MVP).
     - `CEL_MAX_AST_NODES` / `CEL_MAX_AST_DEPTH` (bounded to prevent resource abuse).
     - `CEL_MAX_CONTEXT_SIZE` (optional guard on number of symbols).
   - Implementation note: engines should route stage-specific evaluation through
     the shared helper on `BaseValidatorEngine` (for example,
     `run_cel_assertions_for_stages`) so the two-pass pattern (inputs, then
     outputs) remains consistent. Engines may override payload-building helpers
     if they need to shape input/output data before evaluation.

## What to build first, relative to FMU work

Implementing CEL processing is a prerequisite for making FMI outputs actionable in workflows: the FMI validator will emit signals via `ValidatorCatalogEntry`, and assertions (Rulesets) consume those signals via CEL. Building the CEL engine first (or in parallel) is advisable so that FMI outputs can be immediately checked. If timeline forces a choice, ship CEL evaluation for existing validators first, then layer FMI; FMI depends on CEL, not vice versa.

## Examples (for docs/tests)

- Range check: `payload.weight_kg > 0 && payload.weight_kg < 300`
- Missing/optional signal handling: `has(metrics.avg_temp) && metrics.avg_temp < 50`
- String pattern: `startsWith(user.email, "test+") && endsWith(user.email, "@example.com")`
- List check: `all(request.items, i, i.quantity > 0)` and `any(request.items, i, i.sku == "ABC123")`

## Notes for implementation

- Errors: compile/runtime/cast/missing-required signals result in a simple error string surfaced as a finding, as with other validation issues.
- Allowlist: start with the helpers above; design an extension point to add more safely later.
