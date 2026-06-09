"""Tests for the Tabular Validator V2 column-stage CEL evaluator.

The column stage runs once after row validation against deterministic aggregates
computed from canonical typed values. These tests pin the aggregate contract,
numeric-only ``sum``, null/error failure behavior, and assertion-level findings
so cross-row rules cannot silently drift with pandas or celpy behavior.
"""

from __future__ import annotations

from celpy import celtypes as ct
from django.test import SimpleTestCase

from validibot.validations.validators.tabular.column_eval import CODE_ASSERTION_ERROR
from validibot.validations.validators.tabular.column_eval import CODE_ASSERTION_NULL
from validibot.validations.validators.tabular.column_eval import (
    CODE_COLUMN_ASSERTION_FAILED,
)
from validibot.validations.validators.tabular.column_eval import CODE_TIMED_OUT
from validibot.validations.validators.tabular.column_eval import ColumnAssertion
from validibot.validations.validators.tabular.column_eval import build_column_context
from validibot.validations.validators.tabular.column_eval import (
    evaluate_column_assertions,
)
from validibot.validations.validators.tabular.readers.csv import read_csv
from validibot.validations.validators.tabular.schema import parse_table_schema


class ColumnAggregateContextTests(SimpleTestCase):
    """The bound ``col.*`` map uses canonical typed aggregate semantics."""

    def test_numeric_aggregates_count_nulls_and_coercion_errors(self):
        """Invalid and empty numeric cells are nulls, not distinct values.

        The valid values remain numeric, so ``"1"`` and ``"1.0"`` collapse to
        one distinct value and contribute numerically to ``sum``.
        """
        read_result = read_csv(
            b"value,marker\n1,a\n1.0,b\nbad,c\n,d\n3,e\n",
        )
        schema = parse_table_schema(
            {"fields": [{"name": "value", "type": "number"}]},
        )

        context = build_column_context(read_result, schema)
        value = context["value"]

        self.assertEqual(value["distinct_count"], 2)
        self.assertEqual(value["null_count"], 2)
        self.assertEqual(value["non_null_count"], 3)
        self.assertEqual(value["null_ratio"], 0.4)
        self.assertEqual(value["min"], 1.0)
        self.assertEqual(value["max"], 3.0)
        self.assertEqual(value["sum"], 5.0)

    def test_absent_declared_column_has_a_stable_empty_aggregate(self):
        """An optional declared column remains addressable when absent.

        Column presence itself belongs in ``i.column_names``; binding an empty
        aggregate keeps a saved column assertion deterministic for optional data.
        """
        read_result = read_csv(b"present\nA\nB\n")
        schema = parse_table_schema(
            {
                "fields": [
                    {"name": "present", "type": "string"},
                    {"name": "optional", "type": "number"},
                ],
            },
        )

        optional = build_column_context(read_result, schema)["optional"]

        self.assertEqual(optional["null_count"], 2)
        self.assertEqual(optional["non_null_count"], 0)
        self.assertEqual(optional["null_ratio"], 1.0)
        self.assertIsNone(optional["min"])
        # An empty ``number`` column must still expose a *double* sum. ``sum([])``
        # is the Python int ``0``; if it bound as a CEL int, comparing it to a
        # float literal would raise an int/double overload error (regression
        # guard for the all-null numeric column case).
        self.assertEqual(optional["sum"], 0.0)
        self.assertIsInstance(optional["sum"], ct.DoubleType)


class ColumnAssertionEvaluationTests(SimpleTestCase):
    """Column predicates produce one linked finding per failed assertion."""

    def setUp(self):
        """Build one realistic numeric column shared by each assertion test."""
        self.read_result = read_csv(b"depth,marker\n1,a\n2,b\n,c\n4,d\n")
        self.schema = parse_table_schema(
            {"fields": [{"name": "depth", "type": "number"}]},
        )

    def test_passing_column_assertion_produces_no_finding(self):
        """A true aggregate predicate is a clean pass."""
        findings = evaluate_column_assertions(
            self.read_result,
            self.schema,
            [ColumnAssertion("col.depth.null_ratio <= 0.25")],
        )
        self.assertEqual(findings, [])

    def test_failed_column_assertion_uses_message_and_assertion_id(self):
        """A false predicate yields one readable, assertion-linked finding."""
        findings = evaluate_column_assertions(
            self.read_result,
            self.schema,
            [
                ColumnAssertion(
                    "col.depth.distinct_count >= 4",
                    message="Depth coverage is too sparse.",
                    assertion_id=42,
                ),
            ],
        )

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, CODE_COLUMN_ASSERTION_FAILED)
        self.assertEqual(findings[0].message, "Depth coverage is too sparse.")
        self.assertEqual(findings[0].column, "depth")
        self.assertEqual(findings[0].assertion_id, 42)

    def test_empty_number_column_sum_compares_against_a_float(self):
        """An all-empty ``number`` column's ``sum`` evaluates cleanly, not as error.

        Why it matters: ``sum([])`` is the Python int ``0``. If the aggregate were
        bound as a CEL int, ``col.depth.sum < 1.5`` would hit celpy's int/double
        overload gap, raise, and be reported as a spurious ``assertion_error`` on
        an otherwise-fine (if sparse) file. Coercing a numeric ``sum`` to a double
        keeps the empty case a normal pass.
        """
        empty_numeric = read_csv(b"depth,marker\n,a\n,b\n")
        schema = parse_table_schema(
            {"fields": [{"name": "depth", "type": "number"}]},
        )

        findings = evaluate_column_assertions(
            empty_numeric,
            schema,
            [ColumnAssertion("col.depth.sum < 1.5")],
        )

        # 0.0 < 1.5 is true → a clean pass with no finding at all (and crucially
        # not a CODE_ASSERTION_ERROR from a failed numeric comparison).
        self.assertEqual(findings, [])

    def test_null_and_error_results_are_failures(self):
        """Null and invalid aggregate access cannot silently pass validation."""
        findings = evaluate_column_assertions(
            self.read_result,
            self.schema,
            [
                ColumnAssertion("null", assertion_id=1),
                ColumnAssertion("col.depth.not_a_metric > 0", assertion_id=2),
            ],
        )

        self.assertEqual(
            {finding.code for finding in findings},
            {CODE_ASSERTION_NULL, CODE_ASSERTION_ERROR},
        )


class ColumnGuardAndBudgetTests(SimpleTestCase):
    """The ``when`` guard scopes a column assertion, and aggregation is bounded."""

    def setUp(self):
        """One numeric column shared across the guard/budget tests."""
        self.read_result = read_csv(b"depth,marker\n1,a\n2,b\n,c\n4,d\n")
        self.schema = parse_table_schema(
            {"fields": [{"name": "depth", "type": "number"}]},
        )

    def test_guard_false_skips_the_assertion(self):
        """A false ``when`` guard skips the whole column assertion.

        The generic lane skips column assertions, so their guard never applied
        before. Here a guard that does not hold means the aggregate predicate is
        not evaluated at all — a would-be failure produces no finding.
        """
        findings = evaluate_column_assertions(
            self.read_result,
            self.schema,
            [
                ColumnAssertion(
                    expression="col.depth.distinct_count >= 100",  # would fail
                    when_expression="1 > 2",  # guard false → skip
                ),
            ],
        )
        self.assertEqual(findings, [])

    def test_guard_error_fails_the_assertion(self):
        """A guard that cannot evaluate fails the assertion, never a silent skip."""
        findings = evaluate_column_assertions(
            self.read_result,
            self.schema,
            [
                ColumnAssertion(
                    expression="col.depth.max <= 10",
                    when_expression='"x" > 1',  # string/int compare → error
                    assertion_id=7,
                ),
            ],
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].code, CODE_ASSERTION_ERROR)
        self.assertEqual(findings[0].assertion_id, 7)

    def test_aggregation_budget_exhaustion_reports_timeout(self):
        """Aggregation honours a wall-clock budget and fails closed on timeout.

        With an already-spent budget the per-column scan stops at its first
        check and the stage reports one ``tabular.timed_out`` finding instead of
        running unbounded (the column lane previously had no budget at all).
        """
        findings = evaluate_column_assertions(
            self.read_result,
            self.schema,
            [ColumnAssertion(expression="col.depth.sum < 1.5")],
            wall_clock_budget_s=-1.0,
        )
        self.assertEqual([f.code for f in findings], [CODE_TIMED_OUT])

    def test_only_referenced_columns_are_aggregated(self):
        """The ``col`` map binds only the columns an assertion references.

        Aggregating every declared column scales the cost with the schema rather
        than the rules — a resource-exhaustion path for a wide schema. Passing
        the referenced set restricts the work to the columns actually used.
        """
        read_result = read_csv(b"a,b\n1,2\n3,4\n")
        schema = parse_table_schema(
            {
                "fields": [
                    {"name": "a", "type": "integer"},
                    {"name": "b", "type": "integer"},
                ],
            },
        )
        context = build_column_context(read_result, schema, {"a"})
        self.assertIn("a", context)
        self.assertNotIn("b", context)
