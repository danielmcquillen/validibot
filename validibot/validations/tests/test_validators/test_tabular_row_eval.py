"""
Tests for the Tabular Validator's row-stage CEL engine
(``validators/tabular/row_eval.py``).

### What this suite covers and why

The row engine is the performance-critical core: it compiles each row assertion
once and evaluates it against every row, binding typed ``row.*`` values. The
behaviours pinned here are the ones ADR-2026-05-26 makes load-bearing:

- **Typed binding** — a number column compares numerically, a date column
  compares as a timestamp; ``now()`` is the pinned run clock, not the wall clock.
- **Null/error-as-failure** — an assertion that evaluates to null or raises is a
  *failure* with a distinct code, never a silent pass (a null cell must not
  satisfy a comparison).
- **Aggregated reporting** — one finding per assertion per outcome class, with a
  count and capped 1-based sample rows.
- **Compile failures are findings**, not crashes.

These are pure functions (no DB); inputs are built through the real ``read_csv``
reader and ``parse_table_schema``.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from django.test import SimpleTestCase

from validibot.validations.constants import Severity
from validibot.validations.validators.tabular.readers.csv import read_csv
from validibot.validations.validators.tabular.row_eval import CODE_ASSERTION_ERROR
from validibot.validations.validators.tabular.row_eval import CODE_ASSERTION_NULL
from validibot.validations.validators.tabular.row_eval import (
    CODE_ROW_ASSERTION_COMPILE_ERROR,
)
from validibot.validations.validators.tabular.row_eval import CODE_ROW_ASSERTION_FAILED
from validibot.validations.validators.tabular.row_eval import RowAssertion
from validibot.validations.validators.tabular.row_eval import evaluate_row_assertions
from validibot.validations.validators.tabular.schema import parse_table_schema

if TYPE_CHECKING:
    from validibot.validations.validators.tabular.native import NativeFinding


def _by_code(findings: list[NativeFinding]) -> dict[str, NativeFinding]:
    return {finding.code: finding for finding in findings}


class RowEvalBasicsTests(SimpleTestCase):
    """Pass/fail of a typed numeric row assertion."""

    def test_passing_assertion_produces_no_findings(self):
        """A row assertion true for every row yields nothing — the baseline
        that proves the engine doesn't false-positive.
        """
        read_result = read_csv(b"lat\n10\n-5\n")
        schema = parse_table_schema(
            {"fields": [{"name": "lat", "type": "number"}]},
        )
        findings = evaluate_row_assertions(
            read_result,
            schema,
            [RowAssertion(expression="row.lat >= -90 && row.lat <= 90")],
        )
        self.assertEqual(findings, [])

    def test_failing_rows_aggregate_into_one_finding(self):
        """Rows where the predicate is false aggregate into a single finding
        carrying the assertion's message/severity, the count, sample rows, and
        the assertion id — not one finding per row.
        """
        read_result = read_csv(b"lat\n10\n200\n-95\n")
        schema = parse_table_schema(
            {"fields": [{"name": "lat", "type": "number"}]},
        )
        findings = evaluate_row_assertions(
            read_result,
            schema,
            [
                RowAssertion(
                    expression="row.lat >= -90 && row.lat <= 90",
                    message="Latitude out of range.",
                    severity=Severity.ERROR,
                    assertion_id=42,
                ),
            ],
        )
        finding = _by_code(findings)[CODE_ROW_ASSERTION_FAILED]
        self.assertEqual(finding.message, "Latitude out of range.")
        self.assertEqual(finding.count, 2)
        self.assertEqual(finding.sample_rows, (2, 3))
        self.assertEqual(finding.assertion_id, 42)

    def test_assertion_specific_example_limit_caps_only_samples(self):
        """A custom limit keeps the full count while shortening diagnostics."""
        read_result = read_csv(b"lat\n-1\n-2\n-3\n")
        schema = parse_table_schema(
            {"fields": [{"name": "lat", "type": "number"}]},
        )
        findings = evaluate_row_assertions(
            read_result,
            schema,
            [
                RowAssertion(
                    expression="row.lat >= 0",
                    report_max_examples=2,
                ),
            ],
        )

        finding = _by_code(findings)[CODE_ROW_ASSERTION_FAILED]
        self.assertEqual(finding.count, 3)
        self.assertEqual(finding.sample_rows, (1, 2))

    def test_cross_field_comparison(self):
        """A cross-field row assertion (``row.min <= row.max``) flags the row
        where it's violated — the canonical reason CEL exists alongside the
        native structured checks.
        """
        read_result = read_csv(b"lo,hi\n1,2\n5,3\n")
        schema = parse_table_schema(
            {
                "fields": [
                    {"name": "lo", "type": "number"},
                    {"name": "hi", "type": "number"},
                ]
            },
        )
        findings = evaluate_row_assertions(
            read_result,
            schema,
            [RowAssertion(expression="row.lo <= row.hi")],
        )
        self.assertEqual(
            _by_code(findings)[CODE_ROW_ASSERTION_FAILED].sample_rows, (2,)
        )


class RowEvalNowTests(SimpleTestCase):
    """``now()`` is the pinned run clock, enabling deterministic time checks."""

    def test_now_is_pinned_for_not_in_future_check(self):
        """``row.eventDate <= now()`` flags only rows after the pinned clock —
        and the result is determined by the supplied clock, not the real time.
        """
        read_result = read_csv(b"eventDate\n2019-01-01\n2099-01-01\n")
        schema = parse_table_schema(
            {"fields": [{"name": "eventDate", "type": "date"}]},
        )
        findings = evaluate_row_assertions(
            read_result,
            schema,
            [RowAssertion(expression="row.eventDate <= now()")],
            now=datetime(2025, 1, 1, tzinfo=UTC),
        )
        self.assertEqual(
            _by_code(findings)[CODE_ROW_ASSERTION_FAILED].sample_rows, (2,)
        )

    def test_now_unbound_without_clock_is_an_error_not_wall_clock(self):
        """With no clock supplied, ``now()`` is unbound, so an assertion using
        it fails as an error rather than silently reading the wall clock.
        """
        read_result = read_csv(b"eventDate\n2019-01-01\n")
        schema = parse_table_schema(
            {"fields": [{"name": "eventDate", "type": "date"}]},
        )
        findings = evaluate_row_assertions(
            read_result,
            schema,
            [RowAssertion(expression="row.eventDate <= now()")],
            now=None,
        )
        self.assertIn(CODE_ASSERTION_ERROR, _by_code(findings))


class RowEvalNullErrorTests(SimpleTestCase):
    """Null/error results are failures with distinct codes, never silent passes."""

    def test_null_cell_does_not_silently_pass(self):
        """A null cell referenced by a comparison must NOT satisfy it. The row
        is recorded as a failure (null or error class), never a pass — the
        load-bearing rule that stops a garbage cell from validating.
        """
        # Row 2 has an empty lat field (a null cell, not a blank line).
        read_result = read_csv(b"lat,v\n10,1\n,2\n")
        schema = parse_table_schema(
            {"fields": [{"name": "lat", "type": "number"}]},
        )
        findings = evaluate_row_assertions(
            read_result,
            schema,
            [RowAssertion(expression="row.lat >= 0")],
        )
        # row 2 must be flagged — accept either the null or error class, but it
        # must not be a silent pass.
        flagged_rows = {
            row
            for finding in findings
            if finding.code in {CODE_ASSERTION_NULL, CODE_ASSERTION_ERROR}
            for row in finding.sample_rows
        }
        self.assertIn(2, flagged_rows)

    def test_compile_error_is_a_finding(self):
        """A row assertion that doesn't compile becomes a single finding rather
        than crashing the whole pass.
        """
        read_result = read_csv(b"a\n1\n")
        schema = parse_table_schema({"fields": [{"name": "a", "type": "number"}]})
        findings = evaluate_row_assertions(
            read_result,
            schema,
            [RowAssertion(expression="row.a >>>")],  # invalid CEL
        )
        self.assertIn(CODE_ROW_ASSERTION_COMPILE_ERROR, _by_code(findings))


class RowEvalMultipleTests(SimpleTestCase):
    """Several assertions evaluated in one pass."""

    def test_multiple_assertions_each_report_independently(self):
        """Two assertions are evaluated against every row in a single pass, each
        producing its own finding for the rows it flags.
        """
        read_result = read_csv(b"lat,lon\n200,20\n10,400\n")
        schema = parse_table_schema(
            {
                "fields": [
                    {"name": "lat", "type": "number"},
                    {"name": "lon", "type": "number"},
                ],
            },
        )
        findings = evaluate_row_assertions(
            read_result,
            schema,
            [
                RowAssertion(
                    expression="row.lat >= -90 && row.lat <= 90", assertion_id=1
                ),
                RowAssertion(
                    expression="row.lon >= -180 && row.lon <= 180",
                    assertion_id=2,
                ),
            ],
        )
        failed = [f for f in findings if f.code == CODE_ROW_ASSERTION_FAILED]
        by_assertion = {f.assertion_id: f.sample_rows for f in failed}
        self.assertEqual(by_assertion[1], (1,))  # lat bad on row 1
        self.assertEqual(by_assertion[2], (2,))  # lon bad on row 2


class RowEvalGuardTests(SimpleTestCase):
    """The optional ``when`` guard scopes a row assertion per row.

    The generic CEL lane evaluates an assertion's ``when`` guard, but it
    deliberately skips tabular row assertions — so a guarded row assertion used
    to run on *every* row. The validator must honour the guard itself: a row
    where the guard is false is out of scope, and a guard that cannot evaluate
    fails the row rather than silently suppressing the rule.
    """

    def test_guard_false_skips_the_row(self):
        """A row whose guard is false is not checked — the rule does not apply."""
        read_result = read_csv(b"category,value\nA,-1\nB,-1\n")
        schema = parse_table_schema(
            {
                "fields": [
                    {"name": "category", "type": "string"},
                    {"name": "value", "type": "integer"},
                ],
            },
        )
        findings = evaluate_row_assertions(
            read_result,
            schema,
            [
                RowAssertion(
                    expression="row.value > 0",
                    when_expression='row.category == "A"',
                ),
            ],
        )
        # Only row 1 (category A) is in scope, and it violates; row 2 (B) is
        # skipped by the guard rather than counted as a failure.
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, CODE_ROW_ASSERTION_FAILED)
        self.assertEqual(findings[0].sample_rows, (1,))

    def test_guard_error_fails_the_row(self):
        """A guard that cannot evaluate fails the row, never a silent skip."""
        read_result = read_csv(b"value\n5\n")
        schema = parse_table_schema(
            {"fields": [{"name": "value", "type": "integer"}]},
        )
        findings = evaluate_row_assertions(
            read_result,
            schema,
            [
                # `"x" > 1` is a string/int comparison — an unevaluable guard.
                RowAssertion(
                    expression="row.value > 0",
                    when_expression='"x" > 1',
                ),
            ],
        )
        self.assertEqual([f.code for f in findings], [CODE_ASSERTION_ERROR])
        self.assertEqual(findings[0].count, 1)
