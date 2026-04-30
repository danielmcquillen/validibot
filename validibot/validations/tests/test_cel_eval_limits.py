"""
Tests for the resource limits enforced by ``evaluate_cel_expression``.

### What we're guarding against

The CEL evaluator accepts expression strings and a context dict from
(potentially untrusted) submissions. Without bounds, an attacker can
send a 5 MB recursively nested JSON payload as the context — the
expression itself is trivially short, but ``celpy.json_to_cel()`` has
to walk the entire tree to normalize Python dicts/lists into CEL's
``MapType``/``ListType``, burning real CPU and memory.

Two complementary limits now cover this:

- **Top-level symbols** (pre-existing, unchanged) — how many distinct
  variable *names* the expression can see (``p``, ``s``, ``output`` ...).
- **Depth + total symbols** (new, from this fix) — how much *work*
  normalization has to do across the whole tree, regardless of how
  few top-level names there are.

### Testing strategy

Call ``evaluate_cel_expression`` directly with fabricated contexts
that trip each limit. We patch the module-level constants down to
small values so the test contexts stay readable — a depth-33 dict in
source would be unreadable, but ``max_depth=3`` + a 4-deep dict is
obvious.

The expressions themselves are minimal; the point of these tests is
to verify *rejection* before evaluation runs. We assert on the
``error`` field of ``CelEvaluationResult`` rather than the exception
type, because the public contract is "returns a structured result,
never raises" — callers like the assertion evaluator depend on that.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase

from validibot.validations.cel_eval import evaluate_cel_expression


def _nested_dict(depth: int) -> dict:
    """Build a dict of the form ``{"x": {"x": {"x": ...}}}`` at *depth*.

    Depth 1 = ``{"x": 1}`` (one container, leaf at the value).
    Depth 2 = ``{"x": {"x": 1}}`` (two nested containers).
    Used to construct predictable inputs for the depth check.
    """
    current: dict | int = 1
    for _ in range(depth):
        current = {"x": current}
    assert isinstance(current, dict)
    return current


class CelContextDepthLimitTests(TestCase):
    """Depth limit — guards against recursively nested contexts.

    The risk is a hostile submission with deeply nested JSON that
    costs more to normalize than it does to construct. Python's
    recursion limit would eventually catch pathologically deep
    inputs but at the cost of an uncaught ``RecursionError`` —
    much better to reject cleanly with a structured error.
    """

    def test_context_within_depth_limit_evaluates_normally(self):
        """A context at the maximum allowed depth must still work —
        the check must not be off-by-one and reject legitimate
        real-world payloads.

        Without this test, a future tweak that turns ``>`` into ``>=``
        would silently start rejecting 1-in-X real contexts without
        an obvious failure mode in production.
        """
        # max_depth=3 means depth-1, depth-2, depth-3 are all OK.
        with (
            patch("validibot.validations.cel_eval.CEL_MAX_CONTEXT_DEPTH", 3),
            patch("validibot.validations.cel_eval.CEL_MAX_CONTEXT_TOTAL_SYMBOLS", 1000),
        ):
            result = evaluate_cel_expression(
                expression="p.x.x == 1",
                context={"p": _nested_dict(2)},  # {"p": {"x": {"x": 1}}}
            )

        self.assertTrue(result.success, f"unexpected error: {result.error!r}")
        self.assertTrue(result.value)

    def test_context_exceeding_depth_limit_is_rejected_before_evaluation(self):
        """A context one level past the max must be rejected with a
        clear error, not raise — callers rely on structured results.

        The important behavioural guarantee is that the rejection
        happens *before* ``celpy.json_to_cel()`` runs, i.e. before
        the 5 MB-of-work that this limit exists to prevent. We can
        only assert that indirectly (by observing a non-evaluation
        error message), because celpy's normalization isn't
        instrumented.
        """
        with (
            patch("validibot.validations.cel_eval.CEL_MAX_CONTEXT_DEPTH", 3),
            patch("validibot.validations.cel_eval.CEL_MAX_CONTEXT_TOTAL_SYMBOLS", 1000),
        ):
            result = evaluate_cel_expression(
                expression="true",
                context={"p": _nested_dict(4)},  # 4 > 3 → reject
            )

        self.assertFalse(result.success)
        self.assertIn("nesting depth", result.error.lower())
        self.assertIsNone(result.value)

    def test_deep_list_nesting_triggers_depth_limit(self):
        """Lists count as containers for the depth walk — a nested
        list-of-lists is just as expensive to normalize as a
        nested dict.

        Without this, an attacker who knew the limit only applied
        to dicts could bypass it by wrapping each level in a
        single-element list.
        """
        # Four-deep list: [[[[1]]]] — nested containers at depths 2,3,4,5.
        deeply_nested_list = [[[[1]]]]
        with (
            patch("validibot.validations.cel_eval.CEL_MAX_CONTEXT_DEPTH", 3),
            patch("validibot.validations.cel_eval.CEL_MAX_CONTEXT_TOTAL_SYMBOLS", 1000),
        ):
            result = evaluate_cel_expression(
                expression="true",
                context={"p": deeply_nested_list},
            )

        self.assertFalse(result.success)
        self.assertIn("nesting depth", result.error.lower())


class CelContextTotalSymbolLimitTests(TestCase):
    """Total-symbol limit — guards against wide-but-shallow contexts.

    A flat dict with 100 000 keys stays at depth 1 but still costs
    real CPU to normalize. The top-level ``CEL_MAX_CONTEXT_SYMBOLS``
    bound doesn't catch this (it only counts namespace-level keys):
    one top-level key holding a giant dict looks fine to the
    top-level check but isn't.
    """

    def test_context_within_symbol_limit_evaluates_normally(self):
        """A context at exactly the symbol-count limit must still
        evaluate — again, guards against off-by-one.
        """
        # 5 top-level keys + 5 nested items under "p" = 10 symbols.
        context = {
            "a": 1,
            "b": 2,
            "c": 3,
            "d": 4,
            "p": {"k1": 1, "k2": 2, "k3": 3, "k4": 4, "k5": 5},
        }
        with (
            patch("validibot.validations.cel_eval.CEL_MAX_CONTEXT_DEPTH", 32),
            patch("validibot.validations.cel_eval.CEL_MAX_CONTEXT_TOTAL_SYMBOLS", 10),
        ):
            result = evaluate_cel_expression(
                expression="p.k1 == 1",
                context=context,
            )

        self.assertTrue(result.success, f"unexpected error: {result.error!r}")

    def test_context_exceeding_symbol_limit_is_rejected(self):
        """A context with more total symbols than allowed must be
        rejected — this is the "5 MB of shallow JSON" case from the
        ADR.
        """
        # 11 nested symbols under a single top-level key — top-level
        # check passes (just 1 key), but total-symbol check catches it.
        context = {"p": {f"k{i}": i for i in range(11)}}
        with (
            patch("validibot.validations.cel_eval.CEL_MAX_CONTEXT_DEPTH", 32),
            patch("validibot.validations.cel_eval.CEL_MAX_CONTEXT_TOTAL_SYMBOLS", 10),
        ):
            result = evaluate_cel_expression(
                expression="true",
                context=context,
            )

        self.assertFalse(result.success)
        self.assertIn("total symbol count", result.error.lower())

    def test_list_items_count_toward_symbol_limit(self):
        """List items count alongside dict keys in the symbol total —
        a 100 000-element list is as expensive to normalize as a
        100 000-key dict.

        Important defence against bypass: without counting list
        items, an attacker could flip their payload from
        ``{"k1": 1, "k2": 2, ...}`` to ``{"items": [1, 2, 3, ...]}``
        and skip the total-symbol check entirely.
        """
        context = {"items": list(range(11))}  # 11 list items + 1 dict key = 12 symbols
        with (
            patch("validibot.validations.cel_eval.CEL_MAX_CONTEXT_DEPTH", 32),
            patch("validibot.validations.cel_eval.CEL_MAX_CONTEXT_TOTAL_SYMBOLS", 10),
        ):
            result = evaluate_cel_expression(
                expression="true",
                context=context,
            )

        self.assertFalse(result.success)
        self.assertIn("total symbol count", result.error.lower())


class CelContextEdgeCaseTests(TestCase):
    """Edge cases around the shape validator's entry and boundary
    conditions — the spots where off-by-one bugs or Python type
    assumptions tend to hide.
    """

    def test_empty_context_evaluates_literal_expressions(self):
        """An empty context ``{}`` must pass the shape check — zero
        symbols, depth-1 dict, nothing nested. Literal expressions
        ("``1 + 1``") should evaluate without ever needing context
        values.
        """
        result = evaluate_cel_expression(expression="1 + 1", context={})

        self.assertTrue(result.success, f"unexpected error: {result.error!r}")
        self.assertEqual(result.value, 2)

    def test_aliased_containers_are_counted_once(self):
        """Two namespace keys pointing at the SAME underlying dict
        must count that dict's contents once, not twice.

        This is the correctness detail that caught a regression: the
        real CEL context at ``_build_cel_context`` aliases the payload
        under both ``p`` and ``payload`` (and under ``o`` / ``output``
        for output-stage assertions). Without ``id()`` deduplication
        in the walker, a realistic 2 700-symbol THERM XML context is
        counted as ~10 800 symbols (4× the payload) and trips the
        10 000 limit even though there's only 2 700 symbols of actual
        data-under-normalization.

        Pins the aliasing contract: same object under different keys
        = one cost.
        """
        shared = {f"k{i}": i for i in range(6)}  # 6 symbols
        # Four aliases of the same dict — naive counting would be 24.
        # With id() dedup, total should be 6 (plus 4 for the top-level
        # keys themselves).
        with (
            patch("validibot.validations.cel_eval.CEL_MAX_CONTEXT_DEPTH", 32),
            # Tight enough that naive counting (24 + 4 = 28) would
            # trip; loose enough that deduped counting (6 + 4 = 10)
            # passes.
            patch("validibot.validations.cel_eval.CEL_MAX_CONTEXT_TOTAL_SYMBOLS", 15),
        ):
            result = evaluate_cel_expression(
                expression="a.k0 == 0",
                context={"a": shared, "b": shared, "c": shared, "d": shared},
            )

        self.assertTrue(
            result.success,
            f"aliased containers wrongly double-counted: {result.error!r}",
        )

    def test_scalar_values_do_not_count_as_symbols(self):
        """A context of ``{"a": 1, "b": 2}`` counts as 2 symbols,
        not 4 — the scalar values at the leaves are not counted
        themselves, only the dict keys that hold them.

        This keeps the limit interpretable: "symbol count" matches
        the number of things celpy has to create ``MapType`` /
        ``ListType`` entries for, which is the actual cost driver.
        """
        # Exactly 2 symbols — right at the limit.
        with (
            patch("validibot.validations.cel_eval.CEL_MAX_CONTEXT_DEPTH", 32),
            patch("validibot.validations.cel_eval.CEL_MAX_CONTEXT_TOTAL_SYMBOLS", 2),
        ):
            result = evaluate_cel_expression(
                expression="a + b",
                context={"a": 1, "b": 2},
            )

        self.assertTrue(result.success, f"unexpected error: {result.error!r}")
        self.assertEqual(result.value, 3)


# ─────────────────────────────────────────────────────────────────────
# Expression-side AST shape limits — [review-§14.ast_check]
# ─────────────────────────────────────────────────────────────────────
#
# Context shape (above) guards the data the submitter controls.
# Expression shape (below) guards the expression the author controls.
# Different attackers, different fixes — but both sit in the same
# module because they share the same evaluator and the same structured-
# result contract.
#
# The tests below patch ``CEL_MAX_MACRO_NESTING`` and
# ``CEL_MAX_MACRO_COUNT`` down to small numbers so the test
# expressions stay readable. They also clear the ``_compile_expr``
# LRU cache in ``setUp`` because otherwise a test that compiles
# expression X under patched limits could poison the cache for a
# later test that evaluates X under different limits. (lru_cache
# does not cache exceptions, but it *does* cache successful compiles,
# and the shape check runs *during* compile.)


class CelExpressionMacroNestingTests(TestCase):
    """Macro-nesting limit — guards against exponential-time CEL.

    The cel-spec explicitly identifies macro nesting as "the only
    avenue for exponential behavior" in CEL. Six chained ``.all()``
    calls with predicates that each iterate the outer list yields
    10^6 evaluations in a few hundred characters, comfortably inside
    the existing 2000-char expression budget. This class pins the
    AST-level defence that prevents that shape from compiling.
    """

    def setUp(self):
        # See module-level docstring for why we clear.
        from validibot.validations.cel_eval import _compile_expr

        _compile_expr.cache_clear()

    def test_single_macro_passes(self):
        """A single macro with a trivial predicate is the common case —
        ``items.all(i, i > 0)`` — and must never trip the limit.

        Regression guard: if a future refactor accidentally counts
        nesting-depth starting at 1 instead of 0, a single macro
        would spuriously fail. This test pins the "depth 1 is fine"
        contract.
        """
        result = evaluate_cel_expression(
            expression="items.all(i, i > 0)",
            context={"items": [1, 2, 3]},
        )
        self.assertTrue(result.success, f"unexpected error: {result.error!r}")
        self.assertTrue(result.value)

    def test_two_level_nested_macro_passes(self):
        """``items.all(i, i.tags.all(t, t != ''))`` is the canonical
        real-world two-level pattern — collections of objects that
        each contain their own collection — and must evaluate
        normally. The limit is deliberately set so this shape works.

        If this test ever starts failing, the limit has been set too
        tight and legitimate business logic will start breaking.
        """
        ctx = {
            "items": [
                {"tags": ["a", "b"]},
                {"tags": ["c"]},
            ],
        }
        result = evaluate_cel_expression(
            expression='items.all(i, i.tags.all(t, t != ""))',
            context=ctx,
        )
        self.assertTrue(result.success, f"unexpected error: {result.error!r}")
        self.assertTrue(result.value)

    def test_three_level_nested_macro_is_rejected(self):
        """Three levels of nested macros is the shape the research
        specifically called out as exponential — ``outer.all(a,
        middle.all(b, inner.all(c, ...)))`` is O(|outer|³).

        The reject must happen at compile time (before any
        evaluation), so evaluating even against a tiny context
        returns the compile-time error. Important because a defence
        that only fires after normalization is vulnerable to being
        bypassed by small contexts.
        """
        expr = "a.all(x, a.all(y, a.all(z, x + y + z > 0)))"
        result = evaluate_cel_expression(
            expression=expr,
            context={"a": [1, 2]},
        )
        self.assertFalse(result.success)
        self.assertIn("nests macros too deeply", result.error)

    def test_chained_macros_in_receiver_position_are_not_nested(self):
        """``items.all(i, i > 0).filter(x, x < 10)`` is a *chain*:
        two macros where the second's receiver is the first's result.
        This has additive cost (|items| + |result|), not
        multiplicative — so it should pass the nesting check even
        though both macros appear in the same expression.

        This is the subtle correctness test for the walker: a naive
        implementation that increments depth on every descent into
        a macro's subtree would falsely reject this expression.
        Real CEL authors chain macros all the time and we must not
        break them.
        """
        # Four chained macros — still "depth 1" because none is
        # inside another's predicate arg.
        expr = "items.filter(i, i > 0).map(i, i * 2).filter(i, i < 100).all(i, i != 5)"
        # Note: this expression has 4 macros, so we need to keep
        # the total-count limit above 4 while testing nesting=1.
        with (
            patch("validibot.validations.cel_eval.CEL_MAX_MACRO_NESTING", 1),
            patch("validibot.validations.cel_eval.CEL_MAX_MACRO_COUNT", 10),
        ):
            result = evaluate_cel_expression(
                expression=expr,
                context={"items": [1, 2, 3]},
            )
        self.assertTrue(
            result.success,
            "chained macros should not trigger the nesting limit, "
            f"got: {result.error!r}",
        )


class CelExpressionMacroCountTests(TestCase):
    """Macro-count limit — guards against chain-based cost amplification.

    Nesting=1 alone doesn't bound total work: ten chained
    ``.map(x, x + x).map(x, x + x)...`` stages each roughly doubles
    the operating cost. The cel-spec specifically calls out
    ``.map(x,[x+x,x+x]).map(x,[x+x,x+x])...`` as exponential in *both*
    time and space. The count cap catches that pattern even though
    each individual macro is at nesting depth 1.
    """

    def setUp(self):
        from validibot.validations.cel_eval import _compile_expr

        _compile_expr.cache_clear()

    def test_expression_at_macro_count_limit_passes(self):
        """Off-by-one guard: exactly ``CEL_MAX_MACRO_COUNT`` macros
        in a single expression must still compile.

        Without this assertion, a future tweak that turns ``>`` into
        ``>=`` would silently reject real expressions authors have
        already written.
        """
        # Two chained macros, limit set to exactly two.
        expr = "items.filter(i, i > 0).map(i, i * 2)"
        with (
            patch("validibot.validations.cel_eval.CEL_MAX_MACRO_COUNT", 2),
            patch("validibot.validations.cel_eval.CEL_MAX_MACRO_NESTING", 3),
        ):
            result = evaluate_cel_expression(
                expression=expr,
                context={"items": [1, 2, 3]},
            )
        self.assertTrue(result.success, f"unexpected error: {result.error!r}")

    def test_expression_exceeding_macro_count_is_rejected(self):
        """One macro past the cap must be rejected cleanly.

        Specifically guards the "string-doubling" cel-spec example
        at the AST level before evaluation runs — we don't rely on
        the timeout to catch it, because the timeout doesn't
        actually cancel the worker (see [review-§14.thread_pool]).
        """
        # Three chained macros against a limit of two.
        expr = "items.filter(i, i > 0).map(i, i * 2).filter(i, i < 10)"
        with (
            patch("validibot.validations.cel_eval.CEL_MAX_MACRO_COUNT", 2),
            patch("validibot.validations.cel_eval.CEL_MAX_MACRO_NESTING", 3),
        ):
            result = evaluate_cel_expression(
                expression=expr,
                context={"items": [1, 2, 3]},
            )
        self.assertFalse(result.success)
        self.assertIn("too many macro calls", result.error)


class CelExpressionMacroDetectionTests(TestCase):
    """Correctness tests for ``_is_macro_call`` — the walker has to
    recognize every CEL macro method and must NOT mistakenly count
    regular method calls.

    If the detection function drifts (e.g. a new celpy macro is
    added to the supported set but not to our allowlist), legitimate
    expressions will either leak past the guard (new macro not
    counted) or break (if we miss a known one). These tests pin the
    current allowlist.
    """

    def setUp(self):
        from validibot.validations.cel_eval import _compile_expr

        _compile_expr.cache_clear()

    def test_non_macro_method_calls_do_not_count_toward_macro_limits(self):
        """``items.size()`` and similar non-macro methods must not be
        counted by the macro limit. A naive "count every
        member_dot_arg node" implementation would incorrectly flag
        ``items.size()`` as a macro call.

        Important because ``size()``, ``startsWith()``, ``matches()``
        etc. appear in nearly every real CEL assertion — if they
        counted, the count limit would be useless.

        We assert on the *shape* error messages specifically rather
        than ``result.success`` because a pathological expression
        can fail for non-shape reasons (e.g. type errors). The
        contract we're pinning is only "the shape check doesn't
        fire on non-macro methods."
        """
        # Five non-macro method calls, all using valid CEL string
        # operations — would exceed a macro limit of 1 if they were
        # incorrectly classified as macros.
        expr = (
            "name.size() > 0 && "
            'email.startsWith("a") && '
            'id.endsWith("3") && '
            'email.matches("^[^@]+@[^@]+$") && '
            "s.size() < 100"
        )
        with (
            patch("validibot.validations.cel_eval.CEL_MAX_MACRO_COUNT", 1),
            patch("validibot.validations.cel_eval.CEL_MAX_MACRO_NESTING", 1),
        ):
            result = evaluate_cel_expression(
                expression=expr,
                context={
                    "name": "alice",
                    "email": "a@b.com",
                    "id": "u_123",
                    "s": "hi",
                },
            )
        shape_rejected = (
            "too many macro calls" in result.error
            or "nests macros too deeply" in result.error
        )
        self.assertFalse(
            shape_rejected,
            f"non-macro method calls wrongly flagged as macros: {result.error!r}",
        )

    def test_each_macro_type_is_detected(self):
        """Every CEL macro method must be recognised — if celpy adds
        a new macro that we forget to add to ``_CEL_MACRO_METHODS``,
        this test won't directly catch it, but verifying the full
        known set here pins the current allowlist and forces anyone
        adjusting it to read this test.
        """
        # Build minimal expressions that use each macro — compile
        # each one individually so we can attribute any failure to
        # the specific macro.
        from validibot.validations.cel_eval import _CEL_MACRO_METHODS

        # all/exists/exists_one/map/filter are spec macros with
        # (iter_var, predicate) signatures. reduce is celpy-only
        # and takes 4 args, so we exercise the detection logic via
        # the spec-standard ones and just assert reduce is in the
        # allowlist (without compiling a reduce call, whose arg
        # shape differs).
        spec_macros = {"all", "exists", "exists_one", "map", "filter"}
        for macro in sorted(spec_macros):
            expr = f"xs.{macro}(i, i > 0)"
            with (
                patch("validibot.validations.cel_eval.CEL_MAX_MACRO_COUNT", 1),
                patch("validibot.validations.cel_eval.CEL_MAX_MACRO_NESTING", 1),
            ):
                result = evaluate_cel_expression(
                    expression=expr,
                    context={"xs": [1, 2, 3]},
                )
            # It must compile (within limits). We don't care about
            # the resulting value — some macros error on this shape,
            # some succeed. We care that the SHAPE check passes.
            shape_rejected = (
                "too many macro calls" in result.error
                or "nests macros too deeply" in result.error
            )
            self.assertFalse(
                shape_rejected,
                f"macro {macro!r} wrongly flagged as shape violation: {result.error!r}",
            )

        # Pin the allowlist itself.
        self.assertEqual(
            _CEL_MACRO_METHODS,
            frozenset({"all", "exists", "exists_one", "map", "filter", "reduce"}),
            "macro allowlist drifted — update this test deliberately, "
            "not as a side-effect of another change",
        )
