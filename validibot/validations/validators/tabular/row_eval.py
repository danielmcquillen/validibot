"""Row-stage CEL evaluation — the performance-critical per-row loop.

Evaluates each row CEL assertion against every data row, binding the row's typed
values into the ``row.*`` namespace (plus ``s.*`` workflow signals and ``i.*``
dataset metadata). Following ADR-2026-05-26's strategy, each assertion's program
is compiled **once per run** and evaluated per row with **no per-eval
``ThreadPoolExecutor``**; cost is bounded by the reader's row/assertion caps, the
author-time expression-shape limits (enforced at compile), and a single
wall-clock budget for the whole pass.

Determinism rules the ADR pins:

- ``now()`` resolves to the pinned run clock, not the wall clock.
- An assertion that evaluates to ``null`` or raises is a **failure** with a
  distinct code (``tabular.assertion_null`` / ``tabular.assertion_error``),
  never a silent pass — a garbage cell must not satisfy ``row.a <= row.b`` by
  comparing as null.

Row values are typed via the shared :mod:`coercion` module (the same coercion
native validation uses), so the two lanes agree on what a cell *is*. A cell
that is empty or fails coercion binds as CEL ``null``.
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

from validibot.validations.cel_eval import compile_program
from validibot.validations.constants import Severity
from validibot.validations.validators.tabular.coercion import coerce_cell
from validibot.validations.validators.tabular.native import DEFAULT_REPORT_MAX_EXAMPLES
from validibot.validations.validators.tabular.native import NativeFinding

if TYPE_CHECKING:
    from validibot.validations.validators.tabular.readers.csv import ReadResult
    from validibot.validations.validators.tabular.schema import TabularSchema

# ── Row-stage finding codes (prefix ``tabular.``; never ``csv.``) ───────
CODE_ROW_ASSERTION_FAILED = "tabular.row_assertion_failed"
CODE_ASSERTION_NULL = "tabular.assertion_null"
CODE_ASSERTION_ERROR = "tabular.assertion_error"
CODE_ROW_ASSERTION_COMPILE_ERROR = "tabular.row_assertion_compile_error"
CODE_TIMED_OUT = "tabular.timed_out"

# Check the wall-clock budget every N rows rather than every row, so the check
# itself doesn't dominate a tight loop.
_WALL_CLOCK_CHECK_INTERVAL = 5000
_DEFAULT_WALL_CLOCK_BUDGET_S = 60.0


@dataclass(frozen=True)
class RowAssertion:
    """A single row CEL assertion to evaluate against every row.

    The engine takes plain specs (not model rows) so it stays decoupled from the
    assertion model and unit-testable. ``message`` and ``severity`` shape the
    finding for failing rows; ``assertion_id`` links the finding back to its
    ``RulesetAssertion``.
    """

    expression: str
    message: str = ""
    severity: Severity = Severity.ERROR
    assertion_id: int | None = None
    report_max_examples: int = DEFAULT_REPORT_MAX_EXAMPLES
    # Optional ``when`` guard. The generic CEL lane evaluates this for dataset
    # assertions, but it deliberately skips row/column assertions, so the
    # validator must honour the guard itself: a row where the guard is false is
    # skipped (the rule does not apply), and a guard that errors fails that row.
    when_expression: str = ""


def _to_cel(value: Any) -> Any:
    """Wrap a coerced Python value as the matching celpy type (or None)."""
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


def _rows_1based(positions: list[int], limit: int) -> tuple[int, ...]:
    """Convert 0-based row positions to capped, 1-based data-row numbers."""
    return tuple(pos + 1 for pos in positions[:limit])


@dataclass
class _Outcomes:
    """Per-assertion accumulator for the three non-pass outcomes."""

    failed: list[int]
    null: list[int]
    errored: list[int]


def evaluate_row_assertions(
    read_result: ReadResult,
    schema: TabularSchema,
    row_assertions: list[RowAssertion],
    *,
    signals: dict[str, Any] | None = None,
    input_signals: dict[str, Any] | None = None,
    now: datetime | None = None,
    wall_clock_budget_s: float = _DEFAULT_WALL_CLOCK_BUDGET_S,
    report_max_examples: int = DEFAULT_REPORT_MAX_EXAMPLES,
) -> list[NativeFinding]:
    """Evaluate *row_assertions* against every row; return aggregated findings.

    Each assertion is compiled once (a compile failure becomes a single finding
    and the assertion is skipped). Rows are then iterated a single time, building
    one typed ``row.*`` context per row and evaluating every compiled program
    against it. Per assertion the engine accumulates rows that returned ``false``
    (the rule was violated), ``null``, or raised, and emits at most one finding
    per outcome class — keeping a million-row failure to one readable finding.
    """
    if not row_assertions:
        return []

    findings: list[NativeFinding] = []
    programs: list[tuple[RowAssertion, celpy.Runner, celpy.Runner | None]] = []
    for assertion in row_assertions:
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
                    code=CODE_ROW_ASSERTION_COMPILE_ERROR,
                    message=f"Row assertion failed to compile: {exc}",
                    severity=Severity.ERROR,
                    assertion_id=assertion.assertion_id,
                ),
            )
            continue
        programs.append((assertion, program, guard))

    if not programs:
        return findings

    # Coerce each declared, present column once into typed celpy values. Row
    # assertions can only reference declared columns (enforced at save time), so
    # binding the declared∩present set bounds the per-row context size.
    present = set(read_result.column_names)
    type_by_name = {field.name: field.type for field in schema.fields}
    relevant_columns = [field.name for field in schema.fields if field.name in present]
    frame = read_result.dataframe
    typed_columns: dict[str, list[Any]] = {
        name: [
            _to_cel(coerce_cell(raw, type_by_name.get(name, "string")).value)
            for raw in frame[name].tolist()
        ]
        for name in relevant_columns
    }

    # ``s.*`` and ``i.*`` are constant across rows — convert once.
    signals_cel = celpy.json_to_cel(signals or {})
    input_cel = celpy.json_to_cel(input_signals or {})

    outcomes = [_Outcomes(failed=[], null=[], errored=[]) for _ in programs]
    num_rows = read_result.num_rows
    started = time.monotonic()
    timed_out = False

    for position in range(num_rows):
        if (
            position % _WALL_CLOCK_CHECK_INTERVAL == 0
            and time.monotonic() - started > wall_clock_budget_s
        ):
            timed_out = True
            break

        row_cel = ct.MapType(
            {
                ct.StringType(name): typed_columns[name][position]
                for name in relevant_columns
            },
        )
        context = {"row": row_cel, "s": signals_cel, "i": input_cel}

        for index, (_assertion, program, guard) in enumerate(programs):
            if not _guard_allows(guard, context, position, outcomes[index]):
                continue
            _classify(program, context, position, outcomes[index])

    findings.extend(
        _build_findings(programs, outcomes, report_max_examples=report_max_examples),
    )
    if timed_out:
        findings.append(
            NativeFinding(
                code=CODE_TIMED_OUT,
                message=(
                    f"Row evaluation exceeded the {wall_clock_budget_s:g}s budget "
                    f"after {position} of {num_rows} rows."
                ),
                severity=Severity.ERROR,
                count=num_rows - position,
            ),
        )
    return findings


def _guard_allows(
    guard: celpy.Runner | None,
    context: dict[str, Any],
    position: int,
    outcome: _Outcomes,
) -> bool:
    """Evaluate the optional ``when`` guard for one row.

    Returns ``True`` when the main predicate should run for this row. Semantics:

    - **no guard** → run.
    - **guard true** → run; **guard false** → skip (the rule does not apply to
      this row — a clean skip, recorded nowhere).
    - **guard null** → skip. A conditional like ``when row.x > 0`` simply does
      not apply to a row whose trigger value is empty/undetermined.
    - **guard errors or is non-boolean** → fail the row (``errored``), never a
      silent skip — an unevaluable guard must not quietly suppress the rule.
    """
    if guard is None:
        return True
    try:
        result = guard.evaluate(context)
    except Exception:
        outcome.errored.append(position)
        return False
    if isinstance(result, ct.BoolType):
        return bool(result)
    if result is None:
        return False
    outcome.errored.append(position)
    return False


def _classify(
    program: celpy.Runner,
    context: dict[str, Any],
    position: int,
    outcome: _Outcomes,
) -> None:
    """Evaluate one program for one row and record the non-pass outcome.

    ``true`` is a pass (nothing recorded). ``false`` is a rule violation. A
    ``null`` result, a raised/returned ``CELEvalError``, or a non-boolean result
    are all failures — recorded distinctly so null/error don't masquerade as a
    clean ``false`` (the determinism contract's null/error-as-failure rule).
    """
    try:
        result = program.evaluate(context)
    except Exception:
        outcome.errored.append(position)
        return
    if isinstance(result, CELEvalError):
        outcome.errored.append(position)
    elif result is None:
        outcome.null.append(position)
    elif isinstance(result, ct.BoolType):
        if not bool(result):
            outcome.failed.append(position)
    else:
        # A row assertion must be a boolean predicate; a non-bool result is a
        # misuse, treated as an error rather than a silent pass.
        outcome.errored.append(position)


def _build_findings(
    programs: list[tuple[RowAssertion, celpy.Runner, celpy.Runner | None]],
    outcomes: list[_Outcomes],
    *,
    report_max_examples: int,
) -> list[NativeFinding]:
    """Turn the per-assertion accumulators into findings (one per outcome class)."""
    findings: list[NativeFinding] = []
    for (assertion, _program, _guard), outcome in zip(programs, outcomes, strict=True):
        example_limit = assertion.report_max_examples or report_max_examples
        if outcome.failed:
            findings.append(
                NativeFinding(
                    code=CODE_ROW_ASSERTION_FAILED,
                    message=assertion.message
                    or f"Row assertion failed: {assertion.expression}",
                    severity=assertion.severity,
                    count=len(outcome.failed),
                    sample_rows=_rows_1based(outcome.failed, example_limit),
                    assertion_id=assertion.assertion_id,
                ),
            )
        if outcome.null:
            findings.append(
                NativeFinding(
                    code=CODE_ASSERTION_NULL,
                    message=(
                        f"Row assertion evaluated to null (treated as a failure): "
                        f"{assertion.expression}"
                    ),
                    severity=Severity.ERROR,
                    count=len(outcome.null),
                    sample_rows=_rows_1based(outcome.null, example_limit),
                    assertion_id=assertion.assertion_id,
                ),
            )
        if outcome.errored:
            findings.append(
                NativeFinding(
                    code=CODE_ASSERTION_ERROR,
                    message=(
                        f"Row assertion raised an error (treated as a failure): "
                        f"{assertion.expression}"
                    ),
                    severity=Severity.ERROR,
                    count=len(outcome.errored),
                    sample_rows=_rows_1based(outcome.errored, example_limit),
                    assertion_id=assertion.assertion_id,
                ),
            )
    return findings
