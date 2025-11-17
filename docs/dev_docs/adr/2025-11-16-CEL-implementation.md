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

2. **Safety and sandboxing**
   - Disable all host-aware functions; only allow pure CEL functions/operators. No filesystem, network, or datetime side-effects (provide deterministic helper functions if needed).
   - Enforce timeouts per assertion evaluation; fail the assertion (and mark the step run errored) on timeout or compilation errors.
   - Limit memory/AST size and depth to prevent resource exhaustion.

3. **Scope for MVP**
   - Static expression evaluation only (no stateful accumulators across time).
   - Support numeric, boolean, string, list/object access, and standard CEL comparison/logical operators.
   - Provide a small set of helper functions:
     - Core: `len`, `has`, `exists` (alias for has), `isEmpty`, `notEmpty`.
     - Math: `abs`, `round`, `ceil`, `floor`, `min`, `max`, `clamp`.
     - Strings: `lower`, `upper`, `contains`, `startsWith`, `endsWith`, `matches` (regex), `trim`, `replace`, `split`, `join`.
     - Collections: `all`, `any`, `map`, `filter` (bounded to safe, deterministic use), `distinct`.
     - Type helpers: `typeOf`, `toNumber`, `toString`, `toBool` (safe casting with failure -> null).
     - Date/time: MVP skip; consider adding later with clear UTC-only semantics if needed.
   - No user-defined functions/macros in MVP; consider later.

4. **Types and validation**
   - Validate assertion references at authoring time: every identifier in the CEL AST must resolve to a `ValidatorCatalogEntry.slug` (or approved helper).
   - Optionally allow typed metadata on catalog entries (value_type) to feed CEL type-checking; cast inputs/outputs to their declared types before evaluation.
   - On run, build the context from the signals present in the step run; if required signals are missing, mark the assertion as failed with a clear reason.

5. **Execution flow**
   - For each step run, after the validator produces outputs, materialize a context: `{slug: value}` for all available signals (inputs, outputs, derived).
   - For each assertion in the step’s Ruleset, compile (cache) and evaluate the CEL expression against the context.
   - Collect findings: on false/exception/timeout, emit a finding with severity/code/path as per the assertion definition; on true, no finding.

6. **Caching and performance**
   - Cache compiled CEL programs per assertion id+version for reuse across runs.
   - Recompile on assertion change; bust cache on Ruleset updates.

7. **Observability**
   - Log assertion errors/timeouts with assertion id, step run id, and short diagnostics; optionally include a redacted snippet of the context keys, not full values, for privacy.

8. **Documentation**
   - Add thorough dev docs covering:
     - The expression context shape and how signals map to `ValidatorCatalogEntry.slug`.
     - The allowed functions/operators, limits (timeout, AST size), and error behaviors.
     - Examples for common checks (range checks, string validation, list ops) and how to reference signals from different run stages.
   - Add user docs with step-by-step examples of writing CEL assertions, including sample expressions for inputs/outputs and guidance on handling missing/optional signals.

## What to build first, relative to FMU work

Implementing CEL processing is a prerequisite for making FMI outputs actionable in workflows: the FMI validator will emit signals via `ValidatorCatalogEntry`, and assertions (Rulesets) consume those signals via CEL. Building the CEL engine first (or in parallel) is advisable so that FMI outputs can be immediately checked. If timeline forces a choice, ship CEL evaluation for existing validators first, then layer FMI; FMI depends on CEL, not vice versa.
