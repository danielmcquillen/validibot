"""Column-stage CEL evaluation for Tabular Validator aggregate assertions.

The V2 ``col.*`` namespace is built from canonical typed values, matching the
native and row lanes. Each declared column exposes deterministic aggregates:
``distinct_count``, ``null_count``, ``non_null_count``, ``null_ratio``,
``min``, ``max``, and ``sum`` for numeric columns.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any

import celpy
from celpy import celtypes as ct
from celpy.evaluation import CELEvalError

from validibot.validations.cel_columns import referenced_column_aggregates
from validibot.validations.cel_eval import compile_program
from validibot.validations.constants import Severity
from validibot.validations.validators.tabular.coercion import coerce_cell
from validibot.validations.validators.tabular.native import NativeFinding

if TYPE_CHECKING:
    from validibot.validations.validators.tabular.readers.csv import ReadResult
    from validibot.validations.validators.tabular.schema import FieldSpec
    from validibot.validations.validators.tabular.schema import TabularSchema

CODE_COLUMN_ASSERTION_FAILED = "tabular.column_assertion_failed"
CODE_ASSERTION_NULL = "tabular.assertion_null"
CODE_ASSERTION_ERROR = "tabular.assertion_error"
CODE_COLUMN_ASSERTION_COMPILE_ERROR = "tabular.column_assertion_compile_error"
CODE_TIMED_OUT = "tabular.timed_out"

_NUMERIC_TYPES = frozenset({"integer", "number"})

# Aggregation shares the native/row lanes' wall-clock-budget shape (checked every
# N cells), so the column stage cannot run unbounded either.
_WALL_CLOCK_CHECK_INTERVAL = 5000
_DEFAULT_WALL_CLOCK_BUDGET_S = 30.0


class _ColumnEvalTimeout(Exception):  # noqa: N818 - internal control-flow signal
    """Raised inside aggregation when the wall-clock budget is exhausted."""


@dataclass(frozen=True)
class ColumnAssertion:
    """One CEL predicate evaluated once against the ``col.*`` aggregate map."""

    expression: str
    message: str = ""
    severity: Severity = Severity.ERROR
    assertion_id: int | None = None
    # Optional ``when`` guard. The generic CEL lane skips column assertions, so
    # the validator evaluates the guard here: a false/null guard skips the whole
    # assertion (it does not apply), a guard that errors fails it.
    when_expression: str = ""


def _to_cel(value: Any) -> Any:
    """Convert one aggregate value to its corresponding celpy type."""
    if value is None:
        return None
    if isinstance(value, bool):
        return ct.BoolType(value)
    if isinstance(value, int):
        return ct.IntType(value)
    if isinstance(value, float):
        return ct.DoubleType(value)
    if isinstance(value, datetime):
        return ct.TimestampType(value)
    return ct.StringType(str(value))


def _empty_aggregate(field: FieldSpec, row_count: int) -> dict[str, Any]:
    """Aggregates for a declared column that is absent from the file.

    Every row is null, computed directly — building (and coercing) a list of
    ``row_count`` empty strings just to count them all as null is pure waste,
    and for a wide schema over a large file that allocation is a real
    resource-exhaustion path.
    """
    aggregate: dict[str, Any] = {
        "distinct_count": 0,
        "null_count": row_count,
        "non_null_count": 0,
        "null_ratio": 1.0 if row_count else 0.0,
        "min": None,
        "max": None,
    }
    if field.type in _NUMERIC_TYPES:
        aggregate["sum"] = 0.0 if field.type == "number" else 0
    return aggregate


def _aggregate_column(
    read_result: ReadResult,
    field: FieldSpec,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Return deterministic aggregates for one declared column."""
    row_count = read_result.num_rows
    if field.name not in read_result.column_names:
        return _empty_aggregate(field, row_count)

    raw_values = read_result.dataframe[field.name].tolist()
    values: list[Any] = []
    null_count = 0
    for index, raw in enumerate(raw_values):
        if (
            deadline is not None
            and index % _WALL_CLOCK_CHECK_INTERVAL == 0
            and time.monotonic() > deadline
        ):
            raise _ColumnEvalTimeout
        coerced = coerce_cell(raw, field.type)
        if coerced.is_null or not coerced.ok:
            null_count += 1
        else:
            values.append(coerced.value)

    aggregate: dict[str, Any] = {
        "distinct_count": len(set(values)),
        "null_count": null_count,
        "non_null_count": len(values),
        "null_ratio": (null_count / row_count) if row_count else 0.0,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }
    if field.type in _NUMERIC_TYPES:
        total = sum(values)
        # A ``number`` column must expose a float ``sum`` even when every cell is
        # null: ``sum([])`` is the Python int ``0``, and binding it as a CEL int
        # would make ``col.x.sum < 1.5`` raise an int/double overload error and
        # fail as an assertion error. ``integer`` columns keep an int sum so they
        # compare cleanly against integer literals.
        aggregate["sum"] = float(total) if field.type == "number" else total
    return aggregate


def build_column_context(
    read_result: ReadResult,
    schema: TabularSchema,
    referenced: set[str] | None = None,
    *,
    deadline: float | None = None,
) -> ct.MapType:
    """Build the nested CEL map bound to ``col``.

    Only columns in *referenced* are aggregated (``None`` means all declared
    columns — the default kept for direct callers/tests). Aggregating every
    declared column when an assertion touches one is wasted work that scales
    with the schema rather than the rules.
    """
    fields = [
        field
        for field in schema.fields
        if referenced is None or field.name in referenced
    ]
    return ct.MapType(
        {
            ct.StringType(field.name): ct.MapType(
                {
                    ct.StringType(name): _to_cel(value)
                    for name, value in _aggregate_column(
                        read_result,
                        field,
                        deadline=deadline,
                    ).items()
                },
            )
            for field in fields
        },
    )


def _column_guard_decision(guard: celpy.Runner, context: dict[str, Any]) -> str:
    """Return ``"run"``, ``"skip"``, or ``"error"`` for a column guard.

    Mirrors the row-stage guard: a false/null guard skips the assertion (it does
    not apply), while a guard that errors or returns a non-boolean fails it
    rather than silently suppressing the rule.
    """
    try:
        result = guard.evaluate(context)
    except Exception:
        return "error"
    if isinstance(result, ct.BoolType):
        return "run" if bool(result) else "skip"
    if result is None:
        return "skip"
    return "error"


def evaluate_column_assertions(
    read_result: ReadResult,
    schema: TabularSchema,
    assertions: list[ColumnAssertion],
    *,
    signals: dict[str, Any] | None = None,
    input_signals: dict[str, Any] | None = None,
    now: datetime | None = None,
    wall_clock_budget_s: float = _DEFAULT_WALL_CLOCK_BUDGET_S,
) -> list[NativeFinding]:
    """Evaluate each column assertion once and return one finding per failure."""
    if not assertions:
        return []

    # Aggregate only the columns the assertions (and their guards) reference, and
    # bound the aggregation by the same wall-clock budget the other lanes use.
    referenced: set[str] = set()
    for assertion in assertions:
        referenced |= referenced_column_aggregates(assertion.expression)
        if assertion.when_expression:
            referenced |= referenced_column_aggregates(assertion.when_expression)
    deadline = time.monotonic() + wall_clock_budget_s
    try:
        col_context = build_column_context(
            read_result,
            schema,
            referenced,
            deadline=deadline,
        )
    except _ColumnEvalTimeout:
        return [
            NativeFinding(
                code=CODE_TIMED_OUT,
                message=(
                    f"Column aggregation exceeded the {wall_clock_budget_s:g}s budget."
                ),
                severity=Severity.ERROR,
            ),
        ]

    context = {
        "col": col_context,
        "s": celpy.json_to_cel(signals or {}),
        "i": celpy.json_to_cel(input_signals or {}),
    }
    findings: list[NativeFinding] = []
    for assertion in assertions:
        try:
            program = compile_program(assertion.expression, now=now)
            guard = (
                compile_program(assertion.when_expression, now=now)
                if assertion.when_expression
                else None
            )
        except Exception as exc:
            findings.append(
                NativeFinding(
                    code=CODE_COLUMN_ASSERTION_COMPILE_ERROR,
                    message=f"Column assertion failed to compile: {exc}",
                    assertion_id=assertion.assertion_id,
                ),
            )
            continue

        columns = sorted(referenced_column_aggregates(assertion.expression))
        column_label = ", ".join(columns) or None
        if guard is not None:
            decision = _column_guard_decision(guard, context)
            if decision == "skip":
                continue
            if decision == "error":
                findings.append(
                    NativeFinding(
                        code=CODE_ASSERTION_ERROR,
                        message=(
                            f"Column assertion guard raised an error (treated as "
                            f"a failure): {assertion.when_expression}"
                        ),
                        column=column_label,
                        assertion_id=assertion.assertion_id,
                    ),
                )
                continue

        evaluation_error = False
        try:
            result = program.evaluate(context)
        except Exception:
            result = None
            evaluation_error = True

        if (
            evaluation_error
            or isinstance(result, CELEvalError)
            or not isinstance(result, ct.BoolType)
        ):
            is_null = result is None and not evaluation_error
            code = CODE_ASSERTION_NULL if is_null else CODE_ASSERTION_ERROR
            outcome = "evaluated to null" if is_null else "raised an error"
            findings.append(
                NativeFinding(
                    code=code,
                    message=(
                        f"Column assertion {outcome} (treated as a failure): "
                        f"{assertion.expression}"
                    ),
                    column=column_label,
                    assertion_id=assertion.assertion_id,
                ),
            )
        elif not bool(result):
            findings.append(
                NativeFinding(
                    code=CODE_COLUMN_ASSERTION_FAILED,
                    message=assertion.message
                    or f"Column assertion failed: {assertion.expression}",
                    column=column_label,
                    severity=assertion.severity,
                    assertion_id=assertion.assertion_id,
                ),
            )
    return findings
