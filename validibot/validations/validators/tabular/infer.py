"""Schema inference — "drop a sample → get a Table Schema to edit".

Most target users have a *delimited text sample*, not a hand-written
Frictionless descriptor, so inferring a starting schema from a sample is the
fastest common setup path (and, per ADR-2026-05-26, likely the single
highest-value setup feature). This module reads bounded content regardless of
its filename extension, resolves the dialect and column names via the normal
reader, and guesses each column's type from its values.

The result is a *starting point* the author tightens in the settings editor —
inference picks a type, the author adds the ``min``/``max``/``enum``/``required``
constraints. Inference is deliberately conservative: it never invents
constraints, only types, and a column it can't confidently type stays
``string`` (the safe default that accepts anything).

Type inference is locale-free and reuses the same :func:`coerce_cell` the
validator uses, so an inferred type means exactly what validation will later
enforce. Candidate order is widest-first-wins by specificity:
``integer`` ⊂ ``number``, then ``boolean``, then ``date`` — a column of
``"1"``/``"2"`` is ``integer`` (checked before ``boolean``, so ``0``/``1`` reads
as integer, not boolean), ``"1.5"`` is ``number``, ``"true"``/``"false"`` is
``boolean``, ISO dates are ``date``, and anything mixed falls back to ``string``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from validibot.validations.validators.tabular.coercion import coerce_cell
from validibot.validations.validators.tabular.preflight import TabularDialect
from validibot.validations.validators.tabular.preflight import TabularLimits
from validibot.validations.validators.tabular.preflight import TabularReadError
from validibot.validations.validators.tabular.readers.csv import read_csv

# Default number of rows sampled for inference. Enough to type columns
# confidently without reading a whole large file.
DEFAULT_SAMPLE_ROWS = 1000

# Inferred descriptors are returned through a hidden form field before the
# author applies them. Bound the serialized result so hostile header names
# cannot inflate that response or a subsequently stored ruleset.
DEFAULT_MAX_SCHEMA_BYTES = 2 * 1024 * 1024
CODE_INFERRED_SCHEMA_TOO_LARGE = "tabular.inferred_schema_too_large"

# Candidate types tried in order; the first that *all* non-empty values satisfy
# wins. ``string`` is the implicit fallback when none match.
_CANDIDATE_TYPES: tuple[str, ...] = ("integer", "number", "boolean", "date")


@dataclass(frozen=True)
class InferredSchema:
    """The product of inference: a descriptor plus the resolved dialect.

    ``descriptor`` is a Frictionless Table Schema dict (``{"fields": [...]}``)
    ready to store as ``ruleset.rules`` and parse with ``parse_table_schema``.
    ``dialect`` carries the sniffed/declared delimiter and header flag to store
    as ``ruleset.metadata``, so a re-read uses the same dialect inference found.
    """

    descriptor: dict
    dialect: TabularDialect


class InferenceError(TabularReadError):
    """A bounded schema-inference failure after the sample was parsed."""


def _infer_column_type(values: list[str]) -> str:
    """Guess a column's type from its sampled string values.

    Empty cells are ignored (they don't constrain the type). A column that is
    entirely empty — or whose non-empty values don't all fit any candidate —
    stays ``string``.
    """
    non_empty = [value for value in values if value != ""]
    if not non_empty:
        return "string"
    for candidate in _CANDIDATE_TYPES:
        # Non-empty values are never "null", so ``ok`` alone means coercible.
        if all(coerce_cell(value, candidate).ok for value in non_empty):
            return candidate
    return "string"


def infer_table_schema(
    content: bytes,
    *,
    dialect: TabularDialect | None = None,
    limits: TabularLimits | None = None,
    sample_rows: int = DEFAULT_SAMPLE_ROWS,
    max_schema_bytes: int = DEFAULT_MAX_SCHEMA_BYTES,
) -> InferredSchema:
    """Infer a Table Schema descriptor and dialect from a sample of *content*.

    Reads at most *sample_rows* rows (the byte-size cap still applies), resolves
    the dialect and canonical column names through the normal reader, and types
    each column from its sampled values. Raises a ``TabularReadError`` if the
    sample itself can't be read (oversized, undecodable, ragged) — the caller
    surfaces that to the author the same way a validation read error is shown.
    """
    read_result = read_csv(
        content,
        dialect=dialect,
        limits=limits,
        sample_rows=sample_rows,
    )

    fields = [
        {"name": name, "type": _infer_column_type(read_result.dataframe[name].tolist())}
        for name in read_result.column_names
    ]
    descriptor = {"fields": fields}
    serialized_size = len(
        json.dumps(descriptor, separators=(",", ":")).encode("utf-8"),
    )
    if serialized_size > max_schema_bytes:
        msg = (
            f"Inferred schema is {serialized_size} bytes, over the "
            f"{max_schema_bytes}-byte limit."
        )
        raise InferenceError(msg, code=CODE_INFERRED_SCHEMA_TOO_LARGE)

    resolved_dialect = TabularDialect(
        delimiter=read_result.preflight.delimiter,
        has_header=read_result.preflight.has_header,
        encoding=read_result.preflight.encoding,
    )
    return InferredSchema(descriptor=descriptor, dialect=resolved_dialect)
