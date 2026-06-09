"""Column-stage CEL evaluation for Tabular Validator aggregate assertions.

The V2 ``col.*`` namespace is built from canonical typed values, matching the
native and row lanes. Each declared column exposes deterministic aggregates:
``distinct_count``, ``null_count``, ``non_null_count``, ``null_ratio``,
``min``, ``max``, and ``sum`` for numeric columns.
"""

from __future__ import annotations

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

_NUMERIC_TYPES = frozenset({"integer", "number"})


@dataclass(frozen=True)
class ColumnAssertion:
    """One CEL predicate evaluated once against the ``col.*`` aggregate map."""

    expression: str
    message: str = ""
    severity: Severity = Severity.ERROR
    assertion_id: int | None = None


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


def _aggregate_column(
    read_result: ReadResult,
    field: FieldSpec,
) -> dict[str, Any]:
    """Return deterministic aggregates for one declared column."""
    if field.name in read_result.column_names:
        raw_values = read_result.dataframe[field.name].tolist()
    else:
        raw_values = [""] * read_result.num_rows

    values: list[Any] = []
    null_count = 0
    for raw in raw_values:
        coerced = coerce_cell(raw, field.type)
        if coerced.is_null or not coerced.ok:
            null_count += 1
        else:
            values.append(coerced.value)

    row_count = read_result.num_rows
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
) -> ct.MapType:
    """Build the nested CEL map bound to ``col``."""
    return ct.MapType(
        {
            ct.StringType(field.name): ct.MapType(
                {
                    ct.StringType(name): _to_cel(value)
                    for name, value in _aggregate_column(read_result, field).items()
                },
            )
            for field in schema.fields
        },
    )


def evaluate_column_assertions(
    read_result: ReadResult,
    schema: TabularSchema,
    assertions: list[ColumnAssertion],
    *,
    signals: dict[str, Any] | None = None,
    input_signals: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[NativeFinding]:
    """Evaluate each column assertion once and return one finding per failure."""
    if not assertions:
        return []

    context = {
        "col": build_column_context(read_result, schema),
        "s": celpy.json_to_cel(signals or {}),
        "i": celpy.json_to_cel(input_signals or {}),
    }
    findings: list[NativeFinding] = []
    for assertion in assertions:
        try:
            program = compile_program(assertion.expression, now=now)
        except Exception as exc:
            findings.append(
                NativeFinding(
                    code=CODE_COLUMN_ASSERTION_COMPILE_ERROR,
                    message=f"Column assertion failed to compile: {exc}",
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

        columns = sorted(referenced_column_aggregates(assertion.expression))
        column_label = ", ".join(columns) or None
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
