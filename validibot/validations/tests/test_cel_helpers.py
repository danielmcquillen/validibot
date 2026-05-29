"""
Tests for the executable CEL helper bindings
(:mod:`validibot.validations.cel_helpers`) and their end-to-end wiring
through :func:`validibot.validations.cel_eval.evaluate_cel_expression`.

### Why this suite exists

ADR-2026-05-26 (Tabular Validator) surfaced a latent platform gap: CEL
helpers were *documented* (``DEFAULT_HELPERS``) and *allowlisted at
authoring time* (the forms identifier check), but never **bound at
runtime**. An expression like ``is_iso8601(row.eventDate)`` would save
cleanly and then raise an unknown-function error the moment it ran.

Slice 1 closes that gap by adding the third registration — the
executable binding. These tests therefore do two jobs:

1. **Unit-test each helper** for the contract the determinism guarantee
   depends on (locale-free parsing, null-as-failure, pinned ``now()``).
2. **Prove the end-to-end runtime path** — compile + bind + evaluate via
   ``evaluate_cel_expression`` — actually executes the helper. These are
   the regression tests for "saves but fails at runtime": before the
   binding existed they would have raised; now they must return a value.

The pure-function and evaluation paths need no database, so the classes
use ``SimpleTestCase``.
"""

from __future__ import annotations

import math
from datetime import UTC
from datetime import datetime

from celpy import celtypes as ct
from django.test import SimpleTestCase

from validibot.validations.cel import DEFAULT_HELPERS
from validibot.validations.cel_eval import evaluate_cel_expression
from validibot.validations.cel_helpers import V1_CEL_HELPER_NAMES
from validibot.validations.cel_helpers import _make_now
from validibot.validations.cel_helpers import _parse_iso8601
from validibot.validations.cel_helpers import build_cel_functions
from validibot.validations.cel_helpers import cel_abs
from validibot.validations.cel_helpers import cel_is_finite
from validibot.validations.cel_helpers import cel_is_int
from validibot.validations.cel_helpers import cel_is_iso8601
from validibot.validations.cel_helpers import cel_max
from validibot.validations.cel_helpers import cel_mean
from validibot.validations.cel_helpers import cel_min
from validibot.validations.cel_helpers import cel_parse_date
from validibot.validations.cel_helpers import cel_percentile
from validibot.validations.cel_helpers import cel_round
from validibot.validations.cel_helpers import cel_sum

# A CEL boolean used as a *test input* (e.g. "is_finite(BoolType) is
# false"). Building ``BoolType(True)`` requires a positional bool literal —
# acknowledge FBT003 once here rather than at every use site.
_CEL_TRUE_INPUT = ct.BoolType(True)  # noqa: FBT003 - celtype test fixture

# ─────────────────────────────────────────────────────────────────────
# is_iso8601 / _parse_iso8601 — format predicate and the shared parser
# ─────────────────────────────────────────────────────────────────────
#
# The parser is the determinism linchpin: it must accept only ISO 8601,
# never consult the host locale, and treat naive inputs as UTC so two
# operators on two machines agree on the instant.


class ParseIso8601Tests(SimpleTestCase):
    """The shared ISO 8601 parser underneath ``is_iso8601``/``parse_date``."""

    def test_parses_date_only_as_utc_midnight(self):
        """A bare ``YYYY-MM-DD`` must parse to midnight UTC.

        Date-only values are the common Darwin Core / meter-export case;
        anchoring them to UTC (not the host's local midnight) is what
        keeps the parsed instant identical across machines, which the
        signing/determinism contract requires.
        """
        parsed = _parse_iso8601("2020-01-02")
        self.assertEqual(
            parsed,
            datetime(2020, 1, 2, tzinfo=UTC),
        )

    def test_parses_zulu_and_offset_to_same_instant(self):
        """``...Z`` and ``...+00:00`` denote the same instant and must
        parse equal — we should not treat the two ISO spellings of UTC
        as different times.
        """
        zulu = _parse_iso8601("2020-01-02T03:04:05Z")
        offset = _parse_iso8601("2020-01-02T03:04:05+00:00")
        self.assertEqual(zulu, offset)

    def test_naive_datetime_is_assumed_utc(self):
        """A datetime with no offset must be interpreted as UTC, not the
        host's local zone — otherwise the same file would validate
        differently in two timezones, breaking reproducibility.
        """
        parsed = _parse_iso8601("2020-01-02T03:04:05")
        self.assertEqual(parsed.tzinfo, UTC)

    def test_rejects_non_iso_and_empty(self):
        """Garbage, locale-formatted dates, and empty strings must return
        ``None`` so callers can fail the assertion rather than guess.

        ``"01/02/2020"`` is deliberately ambiguous (US vs. EU ordering) —
        exactly the locale-dependence the parser refuses to engage with.
        """
        for bad in ("", "   ", "not-a-date", "01/02/2020", "2020-13-01"):
            with self.subTest(value=bad):
                self.assertIsNone(_parse_iso8601(bad))


class CelIsIso8601Tests(SimpleTestCase):
    """``is_iso8601(value)`` — a boolean format predicate over strings."""

    def test_true_for_valid_iso_string(self):
        """A valid ISO 8601 string returns CEL ``true`` — the basic
        positive contract authors rely on for ``is_iso8601(row.x)``.

        Also pins the *type* of the result (``BoolType``, not a raw Python
        bool), since celpy needs a celtype back from a bound function.
        """
        result = cel_is_iso8601("2020-01-02T00:00:00Z")
        self.assertIsInstance(result, ct.BoolType)
        self.assertTrue(result)

    def test_false_for_invalid_string(self):
        """A non-ISO string returns ``false`` (not an error): the
        predicate's whole job is to *report* validity, so a bad date is a
        ``false`` result, which the assertion then treats as a failure.
        """
        self.assertFalse(cel_is_iso8601("nope"))

    def test_false_for_null_and_non_string(self):
        """``null`` and non-strings return ``false`` rather than raising.

        A null date cell must *fail* a format check, never crash the row
        pass or silently pass. Returning ``false`` keeps the helper total
        (defined for every input) which is what makes the row loop safe.
        """
        for value in (None, 42, ct.DoubleType(1.5), _CEL_TRUE_INPUT):
            with self.subTest(value=value):
                self.assertFalse(cel_is_iso8601(value))


# ─────────────────────────────────────────────────────────────────────
# parse_date — string → timestamp | null
# ─────────────────────────────────────────────────────────────────────


class CelParseDateTests(SimpleTestCase):
    """``parse_date(value)`` for string-typed columns."""

    def test_parses_valid_string_to_timestamp(self):
        """A valid ISO string becomes a CEL ``TimestampType`` that compares
        like a timestamp — this is what lets ``parse_date(row.x) <= now()``
        work for columns left as ``type=string``.
        """
        result = cel_parse_date("2020-01-02T03:04:05Z")
        self.assertIsInstance(result, ct.TimestampType)
        self.assertEqual(result, ct.TimestampType("2020-01-02T03:04:05Z"))

    def test_returns_null_for_unparseable(self):
        """An unparseable string returns CEL ``null`` (``None``).

        Per the determinism contract a later comparison against null is a
        *failure*, never a silent pass — so returning null here, rather
        than a sentinel timestamp, is the safe choice.
        """
        self.assertIsNone(cel_parse_date("garbage"))

    def test_returns_null_for_non_string(self):
        """Non-string inputs (including null) return null — ``parse_date``
        is only meaningful on strings; anything else is a no-op null so
        the assertion fails rather than the helper raising.
        """
        for value in (None, 123, _CEL_TRUE_INPUT):
            with self.subTest(value=value):
                self.assertIsNone(cel_parse_date(value))


# ─────────────────────────────────────────────────────────────────────
# is_finite — numeric guard against NaN / Inf
# ─────────────────────────────────────────────────────────────────────


class CelIsFiniteTests(SimpleTestCase):
    """``is_finite(value)`` — true only for finite numbers."""

    def test_true_for_finite_numbers(self):
        """Ordinary ints and floats are finite. Covers both celpy numeric
        types (``IntType``/``DoubleType``) since both subclass Python
        numerics.
        """
        for value in (0, 1, -3, 1.5, ct.IntType(7), ct.DoubleType(2.5)):
            with self.subTest(value=value):
                self.assertTrue(cel_is_finite(value))

    def test_false_for_nan_and_infinities(self):
        """NaN and ±Inf return ``false`` — the reason this helper exists.

        A meter reading of ``inf`` or ``nan`` should fail an ``is_finite``
        guard so it can't slip into downstream arithmetic as a valid
        number.
        """
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                self.assertFalse(cel_is_finite(value))

    def test_false_for_bool_null_and_non_numbers(self):
        """Booleans, null, and non-numbers return ``false``.

        Booleans are excluded deliberately: although ``bool`` subclasses
        ``int``, ``is_finite(true)`` returning ``true`` would be a
        type-confusion trap. A null cell must also fail the guard.
        """
        for value in (True, False, _CEL_TRUE_INPUT, None, "5", [1, 2]):
            with self.subTest(value=value):
                self.assertFalse(cel_is_finite(value))


# ─────────────────────────────────────────────────────────────────────
# now() / build_cel_functions — the per-run clock binding
# ─────────────────────────────────────────────────────────────────────


class NowBindingTests(SimpleTestCase):
    """``now()`` is a pinned, opt-in injection — not a wall-clock read."""

    def test_make_now_returns_pinned_instant(self):
        """``_make_now`` produces a 0-arg callable returning the exact
        instant it was given — the basis of deterministic time-relative
        assertions.
        """
        clock = datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)
        now_fn = _make_now(clock)
        self.assertEqual(now_fn(), ct.TimestampType(clock))

    def test_make_now_treats_naive_clock_as_utc(self):
        """A naive run clock is interpreted as UTC, matching the parser —
        so ``now()`` and ``parse_date`` agree on the zone and comparisons
        between them are meaningful.
        """
        # The naive datetime is the whole point of this test — it exercises
        # the "assume UTC" branch — so DTZ001 is intentionally suppressed.
        now_fn = _make_now(datetime(2026, 5, 29, 12, 0, 0))  # noqa: DTZ001
        self.assertEqual(
            now_fn(),
            ct.TimestampType(datetime(2026, 5, 29, 12, 0, 0, tzinfo=UTC)),
        )

    def test_now_omitted_when_no_clock_supplied(self):
        """``build_cel_functions()`` with no clock must NOT bind ``now`` —
        an expression that calls ``now()`` then fails cleanly instead of
        silently reading the wall clock, protecting determinism for any
        caller that forgot to pin a clock.
        """
        functions = build_cel_functions()
        self.assertNotIn("now", functions)
        # The stateless helpers are always present (V1 row helpers plus the
        # scalar/aggregate helpers); ``now`` is the only opt-in one.
        self.assertLessEqual(
            {"is_iso8601", "parse_date", "is_finite", "mean", "min", "max"},
            set(functions),
        )

    def test_now_bound_when_clock_supplied(self):
        """Supplying a clock adds ``now`` alongside the stateless helpers —
        this is how the Tabular Validator opts in by passing
        ``run.started_at``.
        """
        functions = build_cel_functions(now=datetime(2026, 1, 1, tzinfo=UTC))
        self.assertIn("now", functions)


# ─────────────────────────────────────────────────────────────────────
# End-to-end: compile + bind + evaluate (the regression for the gap)
# ─────────────────────────────────────────────────────────────────────
#
# These are the tests that would have FAILED before slice 1 wired the
# runtime binding — each helper, invoked through the real evaluation
# path, must now return a value instead of raising an unknown-function
# error.


class CelHelperRuntimeBindingTests(SimpleTestCase):
    """``evaluate_cel_expression`` must actually execute the helpers."""

    def test_is_iso8601_executes_at_runtime(self):
        """``is_iso8601(...)`` runs end-to-end and returns a bool.

        The whole point of slice 1: this expression used to parse and save
        but raise at evaluation time. It must now succeed.
        """
        result = evaluate_cel_expression(
            expression='is_iso8601("2020-01-02")',
            context={},
        )
        self.assertTrue(result.success, f"unexpected error: {result.error!r}")
        self.assertTrue(result.value)

    def test_is_finite_executes_against_context_value(self):
        """``is_finite(p.x)`` resolves a context value and evaluates —
        proving helpers compose with namespaced data, not just literals.
        """
        result = evaluate_cel_expression(
            expression="is_finite(p.x)",
            context={"p": {"x": 1.5}},
        )
        self.assertTrue(result.success, f"unexpected error: {result.error!r}")
        self.assertTrue(result.value)

    def test_parse_date_and_now_compose_in_time_relative_assertion(self):
        """``parse_date(row.eventDate) <= now()`` — the canonical
        "not in the future" tabular check — must evaluate, with ``now()``
        pinned to the supplied clock.

        A past date is not-in-the-future relative to the pinned 2020 clock,
        so the assertion is true.
        """
        result = evaluate_cel_expression(
            expression="parse_date(row.eventDate) <= now()",
            context={"row": {"eventDate": "2019-06-01T00:00:00Z"}},
            now=datetime(2020, 1, 1, tzinfo=UTC),
        )
        self.assertTrue(result.success, f"unexpected error: {result.error!r}")
        self.assertTrue(result.value)

    def test_now_is_pinned_not_wall_clock(self):
        """``now()`` returns the supplied instant, not the real time —
        the determinism guarantee. We pin a clock in the year 2000 and a
        date in 2010; "2010 <= now()" must be FALSE because ``now()`` is
        2000, which it would not be if the wall clock leaked in.
        """
        result = evaluate_cel_expression(
            expression='parse_date("2010-01-01T00:00:00Z") <= now()',
            context={},
            now=datetime(2000, 1, 1, tzinfo=UTC),
        )
        self.assertTrue(result.success, f"unexpected error: {result.error!r}")
        self.assertFalse(
            result.value,
            "now() leaked the wall clock instead of the pinned 2000 instant",
        )

    def test_now_unbound_without_clock_fails_cleanly(self):
        """Calling ``now()`` without a pinned clock must fail with a
        structured error, never read the wall clock.

        This is the safety default: a caller that forgets to pin a clock
        gets a clean failure (which a developer will notice) rather than a
        non-deterministic pass (which would silently corrupt an
        attestation).
        """
        result = evaluate_cel_expression(expression="now()", context={})
        self.assertFalse(result.success)
        self.assertIsNone(result.value)

    def test_parse_date_null_path_fails_the_assertion(self):
        """An unparseable date flows through as null and a comparison
        against it is a *failure*, not a silent pass.

        ``parse_date("garbage") <= now()`` evaluates ``null <= timestamp``.
        Per the null-as-failure rule the assertion must not come back
        ``true``; we assert it is not a truthy success.
        """
        result = evaluate_cel_expression(
            expression='parse_date("garbage") <= now()',
            context={},
            now=datetime(2020, 1, 1, tzinfo=UTC),
        )
        # Either celpy raises on null comparison (success False) or returns
        # a non-true value — both are acceptable "did not silently pass".
        self.assertFalse(
            bool(result.success and result.value),
            "a null date silently satisfied a <= now() comparison",
        )


# ─────────────────────────────────────────────────────────────────────
# Scalar helpers: is_int / abs / round
# ─────────────────────────────────────────────────────────────────────
#
# Verified empirically that NONE of these are celpy builtins (a bare
# environment raises CELEvalError for each), so they need real bindings —
# the same documented-but-unbound gap the V1 helpers had.


class IsIntHelperTests(SimpleTestCase):
    """``is_int(value)`` — integral-number predicate."""

    def test_true_for_integers_and_integral_floats(self):
        """``2`` and ``2.0`` are both integral. The integral-float case is
        the subtle one: a CSV cell read as ``2.0`` should still pass an
        integer guard.
        """
        for value in (ct.IntType(2), ct.DoubleType(2.0), 0, -7):
            with self.subTest(value=value):
                self.assertTrue(cel_is_int(value))

    def test_false_for_non_integral_and_non_numbers(self):
        """Fractional numbers, NaN/Inf, booleans, null, and non-numbers all
        return false — a boolean is excluded even though it subclasses int.
        """
        for value in (ct.DoubleType(2.5), math.nan, math.inf, True, None, "3"):
            with self.subTest(value=value):
                self.assertFalse(cel_is_int(value))


class AbsHelperTests(SimpleTestCase):
    """``abs(value)`` — absolute value, type-preserving."""

    def test_preserves_int_and_double(self):
        """``abs`` keeps an int an int and a double a double, so it composes
        with the strict ``int``/``double`` comparison rules celpy enforces.
        """
        int_result = cel_abs(ct.IntType(-3))
        self.assertIsInstance(int_result, ct.IntType)
        self.assertEqual(int_result, ct.IntType(3))

        double_result = cel_abs(ct.DoubleType(-2.5))
        self.assertIsInstance(double_result, ct.DoubleType)
        self.assertEqual(double_result, ct.DoubleType(2.5))

    def test_null_for_non_number(self):
        """A boolean, null, or non-number yields null so a bad input fails
        the assertion rather than raising mid-loop.
        """
        for value in (True, None, "x"):
            with self.subTest(value=value):
                self.assertIsNone(cel_abs(value))


class RoundHelperTests(SimpleTestCase):
    """``round(value, digits=0)`` — deterministic decimal rounding."""

    def test_rounds_to_digits(self):
        """Rounding to a given number of decimals returns a double."""
        self.assertEqual(
            cel_round(ct.DoubleType(2.567), ct.IntType(1)), ct.DoubleType(2.6)
        )

    def test_digits_defaults_to_zero(self):
        """``round(x)`` with one argument rounds to an integer value
        (returned as a double) — celpy respects the Python default arg.
        """
        self.assertEqual(cel_round(ct.DoubleType(2.4)), ct.DoubleType(2.0))

    def test_banker_rounding_is_documented_behaviour(self):
        """Round-half-to-even is deterministic — ``round(0.5) == 0.0`` and
        ``round(1.5) == 2.0``. We pin it so a future "fix" to half-up is a
        deliberate, visible decision (it would change attested results).
        """
        self.assertEqual(cel_round(ct.DoubleType(0.5)), ct.DoubleType(0.0))
        self.assertEqual(cel_round(ct.DoubleType(1.5)), ct.DoubleType(2.0))

    def test_null_for_non_number(self):
        """Non-numbers (and booleans/null) round to null."""
        self.assertIsNone(cel_round(None))


# ─────────────────────────────────────────────────────────────────────
# Aggregate helpers: mean / sum / min / max / percentile
# ─────────────────────────────────────────────────────────────────────
#
# All operate on a CEL list, ignore nulls, and return a double. Malformed
# input (non-list or a non-numeric element) yields null so a comparison
# fails rather than computing over garbage.


class AggregateHelperTests(SimpleTestCase):
    """The list aggregates used mainly by the V2 column-aggregate stage."""

    def test_mean_ignores_nulls(self):
        """``mean`` averages the non-null numeric items; a null in the list
        is skipped, not treated as zero, matching the documented contract.
        """
        self.assertEqual(
            cel_mean([ct.IntType(1), None, ct.IntType(3)]), ct.DoubleType(2.0)
        )

    def test_sum_of_empty_is_zero_but_malformed_is_null(self):
        """``sum`` of an all-null/empty list is the identity ``0.0``; a
        non-numeric element makes the whole result null (a misuse should
        fail, not silently skip).
        """
        self.assertEqual(cel_sum([None, None]), ct.DoubleType(0.0))
        self.assertIsNone(cel_sum([ct.IntType(1), "x"]))

    def test_min_and_max_ignore_nulls(self):
        """``min``/``max`` consider only non-null numeric items and return a
        double (the function form, distinct from celpy's ``.min()`` method).
        """
        values = [ct.IntType(3), None, ct.IntType(1), ct.IntType(2)]
        self.assertEqual(cel_min(values), ct.DoubleType(1.0))
        self.assertEqual(cel_max(values), ct.DoubleType(3.0))

    def test_min_of_empty_is_null(self):
        """The min of nothing is undefined → null (a failing comparison),
        never a sentinel that could pass a check.
        """
        self.assertIsNone(cel_min([None]))

    def test_percentile_median_by_linear_interpolation(self):
        """``percentile(xs, 50)`` is the median; with an even count it
        interpolates between the two middle values (so [1,2,3,4] → 2.5),
        pinning the interpolation method the result depends on.
        """
        self.assertEqual(
            cel_percentile(
                [ct.IntType(1), ct.IntType(2), ct.IntType(3), ct.IntType(4)],
                ct.IntType(50),
            ),
            ct.DoubleType(2.5),
        )

    def test_percentile_out_of_range_q_is_null(self):
        """A *q* outside 0–100 is a misuse and returns null rather than being
        silently clamped to an endpoint.
        """
        self.assertIsNone(
            cel_percentile([ct.IntType(1), ct.IntType(2)], ct.IntType(150))
        )


# ─────────────────────────────────────────────────────────────────────
# End-to-end: scalar/aggregate helpers execute via evaluate_cel_expression
# ─────────────────────────────────────────────────────────────────────
#
# The regression tests: before this binding, each of these expressions
# parsed and saved but raised an unknown-function error at evaluation.


class ScalarAggregateRuntimeBindingTests(SimpleTestCase):
    """Each newly-bound helper must actually execute through the real path."""

    def test_mean_executes_with_ordered_comparison(self):
        """``mean([...]) > n`` runs end-to-end. Ordered comparison is used
        because celpy allows double-vs-int for ``>`` (but not for ``==``).
        """
        result = evaluate_cel_expression(
            expression="mean([1, 2, 3]) > 1.9",
            context={},
        )
        self.assertTrue(result.success, f"unexpected error: {result.error!r}")
        self.assertTrue(result.value)

    def test_function_min_coexists_with_method_min(self):
        """The bound global ``min([...])`` and celpy's ``[...].min()`` method
        both work — binding the function does not shadow the method.
        """
        func_result = evaluate_cel_expression(
            expression="min([3, 1, 2]) == 1.0", context={}
        )
        self.assertTrue(func_result.success, f"unexpected error: {func_result.error!r}")
        self.assertTrue(func_result.value)

        method_result = evaluate_cel_expression(
            expression="[3, 1, 2].min() == 1", context={}
        )
        self.assertTrue(
            method_result.success, f"unexpected error: {method_result.error!r}"
        )
        self.assertTrue(method_result.value)

    def test_is_int_and_round_execute_at_runtime(self):
        """``is_int`` and ``round`` run end-to-end — they used to save but
        raise, the same gap the V1 helpers had.
        """
        is_int_result = evaluate_cel_expression(expression="is_int(2.0)", context={})
        self.assertTrue(
            is_int_result.success, f"unexpected error: {is_int_result.error!r}"
        )
        self.assertTrue(is_int_result.value)

        round_result = evaluate_cel_expression(
            expression="round(2.567, 1) == 2.6", context={}
        )
        self.assertTrue(
            round_result.success, f"unexpected error: {round_result.error!r}"
        )
        self.assertTrue(round_result.value)

    def test_percentile_executes_against_context_list(self):
        """``percentile`` resolves a list from the context and evaluates —
        proving the aggregates compose with namespaced data, not just
        literals.
        """
        result = evaluate_cel_expression(
            expression="percentile(p.values, 50) == 2.5",
            context={"p": {"values": [1, 2, 3, 4]}},
        )
        self.assertTrue(result.success, f"unexpected error: {result.error!r}")
        self.assertTrue(result.value)

    def test_cel_builtin_duration_still_works_unbound(self):
        """``duration("3600s")`` is a CEL built-in and must keep working — we
        deliberately did NOT bind a custom ``duration``, so the built-in is
        not shadowed.
        """
        result = evaluate_cel_expression(
            expression='duration("3600s") > duration("60s")',
            context={},
        )
        self.assertTrue(result.success, f"unexpected error: {result.error!r}")
        self.assertTrue(result.value)


# ─────────────────────────────────────────────────────────────────────
# Registration coherence — the three registrations must not drift
# ─────────────────────────────────────────────────────────────────────


class CelHelperRegistrationCoherenceTests(SimpleTestCase):
    """Pin the cross-module contract so the three registrations stay in sync.

    The ADR's central implementation note is that a helper needs three
    registrations (docs, allowlist, binding). If a future change adds a
    binding but forgets the docs entry (or vice versa), the helper is
    half-wired again. These tests fail loudly when that happens.
    """

    def test_every_v1_helper_is_documented(self):
        """Each runtime-bound V1 helper name has a ``DEFAULT_HELPERS`` entry.

        Documentation drives the authoring UI's tooltip; a bound-but-
        undocumented helper would be invisible to authors.
        """
        for name in V1_CEL_HELPER_NAMES:
            with self.subTest(helper=name):
                self.assertIn(name, DEFAULT_HELPERS)

    def test_v1_helper_names_are_the_expected_set(self):
        """Pin the V1 inventory exactly (ADR-2026-05-26 invariants block).

        Anyone changing this set must do so deliberately and update the
        ADR's helper inventory — not drift it as a side effect.
        """
        self.assertEqual(
            V1_CEL_HELPER_NAMES,
            frozenset({"is_iso8601", "parse_date", "is_finite", "now"}),
        )

    def test_v1_helpers_are_in_the_canonical_allowlist_source(self):
        """The V1 helpers must belong to the single canonical allowlist set.

        Registration #2 (authoring-time) is centralised: every CEL
        identifier allowlist derives the custom-helper names from
        ``CUSTOM_HELPER_NAMES`` (= ``frozenset(DEFAULT_HELPERS)``) instead
        of hand-listing them. This pins that the V1 helpers are part of
        that single source, so they are accepted everywhere CEL is
        authored. (The end-to-end form-acceptance check lives in
        ``test_forms_ruleset_assertion.py``.)
        """
        from validibot.validations.cel import CUSTOM_HELPER_NAMES
        from validibot.validations.cel import DEFAULT_HELPERS

        self.assertEqual(CUSTOM_HELPER_NAMES, frozenset(DEFAULT_HELPERS))
        self.assertTrue(V1_CEL_HELPER_NAMES <= CUSTOM_HELPER_NAMES)
