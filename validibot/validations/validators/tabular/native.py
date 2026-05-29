"""Native structured validation — the dataframe checked against a schema.

Given the reader's string dataframe and a parsed :class:`TabularSchema`, this
module produces findings for the structured constraints a non-coder declares in
the settings: required columns, per-column type, numeric min/max, string
length, regex pattern, enum membership, nullability, and cross-row uniqueness
(single ``unique`` columns and single/composite ``primaryKey``). It is the
counterpart to the CEL assertion lane — these checks are validated *natively*
against the table, never compiled to CEL (ADR-2026-05-26, "two mechanisms").

Reporting follows the ADR's shape: **one finding per failed check**, carrying
the total failure count and up to ``report_max_examples`` sample row numbers
(1-based, file order) — never one finding per failing row. Findings are
returned as :class:`NativeFinding` (a local, pure type) so this module stays
testable without the validator base; slice 2c maps them onto ``ValidationIssue``.

Uniqueness semantics (pinned by the ADR):

- ``unique`` follows SQL: nulls are exempt (multiple nulls do not collide);
  only repeated *non-null* values are a violation.
- ``primaryKey`` is unique **and** non-null: a null in any key component is its
  own violation (``tabular.primary_key_null``), independent of duplicates.
- Comparison runs on **canonical typed values**, so ``"1"`` and ``"1.0"`` are
  the same key in a numeric column.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING

from validibot.validations.constants import Severity
from validibot.validations.validators.tabular.coercion import coerce_cell

if TYPE_CHECKING:
    from typing import Any

    import pandas as pd

    from validibot.validations.validators.tabular.readers.csv import ReadResult
    from validibot.validations.validators.tabular.schema import FieldSpec
    from validibot.validations.validators.tabular.schema import TabularSchema

# ── Native finding codes (prefix ``tabular.``; never ``csv.``) ──────────
CODE_MISSING_REQUIRED_COLUMN = "tabular.missing_required_column"
CODE_REQUIRED_VALUE_MISSING = "tabular.required_value_missing"
CODE_TYPE_ERROR = "tabular.type_error"
CODE_OUT_OF_RANGE = "tabular.out_of_range"
CODE_LENGTH_ERROR = "tabular.length_error"
CODE_PATTERN_MISMATCH = "tabular.pattern_mismatch"
CODE_ENUM_VIOLATION = "tabular.enum_violation"
CODE_INVALID_PATTERN = "tabular.invalid_pattern"
CODE_UNIQUE_VIOLATION = "tabular.unique_violation"
CODE_PRIMARY_KEY_NULL = "tabular.primary_key_null"

_NUMERIC_TYPES = frozenset({"number", "integer"})


@dataclass(frozen=True)
class NativeFinding:
    """One native validation failure, aggregated across rows.

    ``count`` is the total number of rows (or columns, for structural checks)
    that failed; ``sample_rows`` holds up to ``report_max_examples`` 1-based
    data-row numbers (file order) so an author can locate the problem without
    the finding table being flooded by bulk failures.
    """

    code: str
    message: str
    column: str | None = None
    severity: Severity = Severity.ERROR
    count: int = 1
    sample_rows: tuple[int, ...] = dataclass_field(default_factory=tuple)
    # Set for row-stage CEL findings so the issue links back to its
    # RulesetAssertion; left None for structural/native checks.
    assertion_id: int | None = None


def _rows_1based(positions: list[int], limit: int) -> tuple[int, ...]:
    """Convert 0-based row positions to capped, 1-based data-row numbers."""
    return tuple(pos + 1 for pos in positions[:limit])


def _validate_field(
    field: FieldSpec,
    values: list[str],
    report_max_examples: int,
) -> list[NativeFinding]:
    """Validate one present column's cells against its field spec."""
    findings: list[NativeFinding] = []
    constraints = field.constraints

    null_rows: list[int] = []
    type_error_rows: list[int] = []
    valid: list[tuple[int, Any, str]] = []  # (position, coerced value, raw)

    for position, raw in enumerate(values):
        coerced = coerce_cell(raw, field.type)
        if coerced.is_null:
            null_rows.append(position)
        elif not coerced.ok:
            type_error_rows.append(position)
        else:
            valid.append((position, coerced.value, raw))

    # Nullability: a null cell in a required column is a violation.
    if constraints.required and null_rows:
        findings.append(
            NativeFinding(
                code=CODE_REQUIRED_VALUE_MISSING,
                message=f"Column {field.name!r} is required but has empty cells.",
                column=field.name,
                count=len(null_rows),
                sample_rows=_rows_1based(null_rows, report_max_examples),
            ),
        )

    # Type: a non-empty cell that can't be coerced to the declared type.
    if type_error_rows:
        findings.append(
            NativeFinding(
                code=CODE_TYPE_ERROR,
                message=(
                    f"Column {field.name!r} has values that are not valid {field.type}."
                ),
                column=field.name,
                count=len(type_error_rows),
                sample_rows=_rows_1based(type_error_rows, report_max_examples),
            ),
        )

    findings.extend(
        _validate_value_constraints(field, valid, report_max_examples),
    )
    return findings


def _validate_value_constraints(
    field: FieldSpec,
    valid: list[tuple[int, Any, str]],
    report_max_examples: int,
) -> list[NativeFinding]:
    """Apply min/max, length, pattern, and enum to the valid (typed) cells."""
    findings: list[NativeFinding] = []
    constraints = field.constraints

    # Numeric range — only for numeric types, on the coerced value.
    if field.type in _NUMERIC_TYPES and (
        constraints.minimum is not None or constraints.maximum is not None
    ):
        out_of_range = [
            pos
            for pos, value, _raw in valid
            if (constraints.minimum is not None and value < constraints.minimum)
            or (constraints.maximum is not None and value > constraints.maximum)
        ]
        if out_of_range:
            findings.append(
                NativeFinding(
                    code=CODE_OUT_OF_RANGE,
                    message=(
                        f"Column {field.name!r} has values outside the allowed "
                        f"range [{constraints.minimum}, {constraints.maximum}]."
                    ),
                    column=field.name,
                    count=len(out_of_range),
                    sample_rows=_rows_1based(out_of_range, report_max_examples),
                ),
            )

    # String length — on the raw string.
    if constraints.min_length is not None or constraints.max_length is not None:
        bad_length = [
            pos
            for pos, _value, raw in valid
            if (
                constraints.min_length is not None and len(raw) < constraints.min_length
            )
            or (
                constraints.max_length is not None and len(raw) > constraints.max_length
            )
        ]
        if bad_length:
            findings.append(
                NativeFinding(
                    code=CODE_LENGTH_ERROR,
                    message=(
                        f"Column {field.name!r} has values whose length is "
                        f"outside [{constraints.min_length}, {constraints.max_length}]."
                    ),
                    column=field.name,
                    count=len(bad_length),
                    sample_rows=_rows_1based(bad_length, report_max_examples),
                ),
            )

    # Regex pattern — full-match on the raw string (Table Schema semantics).
    if constraints.pattern is not None:
        findings.extend(
            _validate_pattern(field, valid, constraints.pattern, report_max_examples),
        )

    # Enum — raw string membership.
    if constraints.enum is not None:
        allowed = set(constraints.enum)
        not_allowed = [pos for pos, _value, raw in valid if raw not in allowed]
        if not_allowed:
            findings.append(
                NativeFinding(
                    code=CODE_ENUM_VIOLATION,
                    message=(
                        f"Column {field.name!r} has values not in the allowed set."
                    ),
                    column=field.name,
                    count=len(not_allowed),
                    sample_rows=_rows_1based(not_allowed, report_max_examples),
                ),
            )

    return findings


def _validate_pattern(
    field: FieldSpec,
    valid: list[tuple[int, Any, str]],
    pattern: str,
    report_max_examples: int,
) -> list[NativeFinding]:
    """Full-match each raw value against *pattern*; flag non-matches."""
    try:
        compiled = re.compile(pattern)
    except re.error:
        # An invalid regex is a configuration error, not a data error — surface
        # it once rather than silently skipping the check.
        return [
            NativeFinding(
                code=CODE_INVALID_PATTERN,
                message=f"Column {field.name!r} has an invalid regex pattern.",
                column=field.name,
            ),
        ]
    mismatched = [pos for pos, _value, raw in valid if compiled.fullmatch(raw) is None]
    if not mismatched:
        return []
    return [
        NativeFinding(
            code=CODE_PATTERN_MISMATCH,
            message=f"Column {field.name!r} has values not matching the pattern.",
            column=field.name,
            count=len(mismatched),
            sample_rows=_rows_1based(mismatched, report_max_examples),
        ),
    ]


def _coerced_key(raw: str, field_type: str) -> object | None:
    """Return the hashable canonical key for a cell, or None if null/invalid.

    A null or type-errored cell yields ``None`` so uniqueness can treat it as
    "no key" (exempt for ``unique``; a violation for ``primaryKey``).
    """
    coerced = coerce_cell(raw, field_type)
    if coerced.is_null or not coerced.ok:
        return None
    return coerced.value


def _validate_single_unique(
    field: FieldSpec,
    values: list[str],
    report_max_examples: int,
) -> list[NativeFinding]:
    """Single-column ``unique``: repeated non-null typed values are violations.

    Nulls are exempt (SQL ``UNIQUE`` semantics); type-errored cells are skipped
    here because they are already reported as type errors.
    """
    positions_by_key: dict[object, list[int]] = {}
    for position, raw in enumerate(values):
        key = _coerced_key(raw, field.type)
        if key is None:
            continue
        positions_by_key.setdefault(key, []).append(position)

    duplicate_rows = sorted(
        position
        for positions in positions_by_key.values()
        if len(positions) > 1
        for position in positions
    )
    if not duplicate_rows:
        return []
    return [
        NativeFinding(
            code=CODE_UNIQUE_VIOLATION,
            message=f"Column {field.name!r} has duplicate values (must be unique).",
            column=field.name,
            count=len(duplicate_rows),
            sample_rows=_rows_1based(duplicate_rows, report_max_examples),
        ),
    ]


def _validate_primary_key(
    schema: TabularSchema,
    frame: pd.DataFrame,
    present: set[str],
    report_max_examples: int,
) -> list[NativeFinding]:
    """Validate ``primaryKey``: each part non-null, and the tuple unique.

    Skipped entirely when any key column is absent — that absence is reported
    once as a missing required column by the caller.
    """
    pk = schema.primary_key
    if not pk or any(column not in present for column in pk):
        return []

    type_by_name = {f.name: f.type for f in schema.fields}
    columns = [frame[column].tolist() for column in pk]

    findings: list[NativeFinding] = []
    null_rows: list[int] = []
    positions_by_tuple: dict[tuple, list[int]] = {}

    for position, raw_values in enumerate(zip(*columns, strict=True)):
        keys = [
            _coerced_key(raw, type_by_name.get(column, "string"))
            for raw, column in zip(raw_values, pk, strict=True)
        ]
        if any(key is None for key in keys):
            null_rows.append(position)
            continue
        positions_by_tuple.setdefault(tuple(keys), []).append(position)

    key_label = ", ".join(pk)
    if null_rows:
        findings.append(
            NativeFinding(
                code=CODE_PRIMARY_KEY_NULL,
                message=(
                    f"Primary key ({key_label}) has empty/invalid values; every "
                    f"key column must be present in every row."
                ),
                column=key_label,
                count=len(null_rows),
                sample_rows=_rows_1based(null_rows, report_max_examples),
            ),
        )

    duplicate_rows = sorted(
        position
        for positions in positions_by_tuple.values()
        if len(positions) > 1
        for position in positions
    )
    if duplicate_rows:
        findings.append(
            NativeFinding(
                code=CODE_UNIQUE_VIOLATION,
                message=f"Primary key ({key_label}) has duplicate rows.",
                column=key_label,
                count=len(duplicate_rows),
                sample_rows=_rows_1based(duplicate_rows, report_max_examples),
            ),
        )
    return findings


def validate_native(
    read_result: ReadResult,
    schema: TabularSchema,
    *,
    report_max_examples: int = 10,
) -> list[NativeFinding]:
    """Validate the dataframe against *schema*; return aggregated findings.

    A field whose column is absent is a missing-column failure only when the
    column is *required to be present* — declared ``required`` or part of the
    primary key. An absent optional column is simply skipped. Present columns
    are checked cell-by-cell (nullability, type, then value constraints), and
    uniqueness runs last over the typed values.
    """
    frame = read_result.dataframe
    present = set(read_result.column_names)
    findings: list[NativeFinding] = []

    # One place decides "this column must exist": required fields + primary key.
    must_be_present = {f.name for f in schema.fields if f.constraints.required} | set(
        schema.primary_key
    )
    for column in sorted(must_be_present - present):
        findings.append(
            NativeFinding(
                code=CODE_MISSING_REQUIRED_COLUMN,
                message=f"Required column {column!r} is missing from the file.",
                column=column,
            ),
        )

    for field in schema.fields:
        if field.name not in present:
            continue
        values = frame[field.name].tolist()
        findings.extend(_validate_field(field, values, report_max_examples))
        if field.constraints.unique:
            findings.extend(
                _validate_single_unique(field, values, report_max_examples),
            )

    findings.extend(
        _validate_primary_key(schema, frame, present, report_max_examples),
    )
    return findings
