"""CSV reader — the V1 front-end that produces the shared in-memory model.

The reader orchestrates PREFLIGHT, resolves the one canonical logical name
per column, and loads the body into a string-valued dataframe with strict
parsing. Everything is read **as strings** (no type/NaN inference); typed
coercion happens later, during native validation and row-stage CEL
evaluation, so parsing stays deterministic and locale-free.

See ADR-2026-05-26 (Tabular Validator): "Parser", "Column-name handling",
"Limits", and the ``i.num_rows ≡ len(df)`` invariant.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pandas as pd
from pandas.errors import EmptyDataError
from pandas.errors import ParserError

from validibot.validations.validators.tabular.preflight import CODE_EMPTY_FILE
from validibot.validations.validators.tabular.preflight import PreflightResult
from validibot.validations.validators.tabular.preflight import TabularDialect
from validibot.validations.validators.tabular.preflight import TabularLimits
from validibot.validations.validators.tabular.preflight import TabularReadError
from validibot.validations.validators.tabular.preflight import run_preflight

# ── READ-stage finding/error codes (prefix ``tabular.``; never ``csv.``) ──
CODE_PARSE_ERROR = "tabular.parse_error"
CODE_TOO_MANY_ROWS = "tabular.too_many_rows"
CODE_DUPLICATE_HEADER = "tabular.duplicate_header"
CODE_BLANK_HEADER = "tabular.blank_header"
CODE_HEADER_CASE_COLLISION = "tabular.header_case_collision"
CODE_HEADER_NAME_TOO_LONG = "tabular.header_name_too_long"
CODE_COLUMN_COUNT_MISMATCH = "tabular.column_count_mismatch"


class ParseError(TabularReadError):
    """A failure detected at READ — parsing the body, not the first record.

    Distinct from ``PreflightError``: a file can pass PREFLIGHT (size,
    encoding, header) and still fail here on ragged rows, unbalanced
    quotes, or row-count overflow deep in the data.
    """


@dataclass(frozen=True)
class ReadResult:
    """The shared in-memory model produced by any reader.

    ``dataframe`` holds every cell as a string (empty cells are ``""``),
    keyed by the canonical ``column_names``. ``num_rows`` is ``len(df)`` —
    the parsed data-row count, never a byte-level newline count (a quoted
    field may contain newlines per RFC 4180).
    """

    dataframe: pd.DataFrame
    column_names: list[str]
    num_rows: int
    num_columns: int
    preflight: PreflightResult


def _canonical_header_names(
    raw_names: list[str],
    *,
    max_name_chars: int,
) -> list[str]:
    """Validate and canonicalise header names; fail on unsafe headers.

    ``row.*`` keys come from these names, so the header must be nailed down
    or the namespace is unsafe. Rules (fail-by-default, per the ADR):

    - leading/trailing whitespace is trimmed; the trimmed form is canonical;
    - a blank/empty name fails (an unnamed column can't be addressed);
    - duplicate names fail (``row.value`` would be ambiguous);
    - case-only collisions fail (Table Schema treats names as
      not-case-sensitive *for uniqueness*, so ``Lat`` vs ``lat`` is a
      collision, not two columns).

    A BOM is already stripped at decode time, so the first name is clean.
    """
    names = [name.strip() for name in raw_names]

    blanks = [i + 1 for i, name in enumerate(names) if name == ""]
    if blanks:
        msg = f"Header has blank/empty column name(s) at position(s): {blanks}."
        raise ParseError(msg, code=CODE_BLANK_HEADER)

    oversized = [i + 1 for i, name in enumerate(names) if len(name) > max_name_chars]
    if oversized:
        msg = (
            f"Header has column name(s) over the {max_name_chars}-character "
            f"limit at position(s): {oversized}."
        )
        raise ParseError(msg, code=CODE_HEADER_NAME_TOO_LONG)

    # Track first occurrence by casefolded key so we can tell an exact
    # duplicate (same bytes) apart from a case-only collision.
    seen: dict[str, str] = {}
    for name in names:
        key = name.casefold()
        if key in seen:
            if seen[key] == name:
                msg = f"Header has a duplicate column name: {name!r}."
                raise ParseError(msg, code=CODE_DUPLICATE_HEADER)
            msg = (
                f"Header has column names that collide ignoring case: "
                f"{seen[key]!r} and {name!r}."
            )
            raise ParseError(msg, code=CODE_HEADER_CASE_COLLISION)
        seen[key] = name

    return names


def _resolve_logical_names(
    *,
    column_count: int,
    header_names: list[str] | None,
    declared_columns: list[str] | None,
    max_header_name_chars: int,
) -> list[str]:
    """Resolve the one canonical logical name for each column position.

    Precedence (ADR-2026-05-26, "Column-name handling"):

    1. headered file → the validated/canonicalised header string;
    2. headerless file with a declared name for that position → the
       declared name (positional alignment);
    3. headerless file otherwise → a synthesised ``column_N`` (1-based).

    So ``column_N`` is the *headerless default*, replaced wherever a name
    is declared — there is no second naming layer.
    """
    if header_names is not None:
        return _canonical_header_names(
            header_names,
            max_name_chars=max_header_name_chars,
        )

    declared = declared_columns or []
    return [
        declared[i] if i < len(declared) else f"column_{i + 1}"
        for i in range(column_count)
    ]


def read_csv(
    content: bytes,
    *,
    dialect: TabularDialect | None = None,
    declared_columns: list[str] | None = None,
    limits: TabularLimits | None = None,
    sample_rows: int | None = None,
) -> ReadResult:
    """Read CSV *content* into the shared in-memory model.

    Runs PREFLIGHT (size/encoding/dialect/first-record), resolves canonical
    column names, then loads the body strictly: every cell as a string, no
    NaN inference, ragged rows and unbalanced quotes raised as a
    :class:`ParseError`. The row cap is enforced here by reading at most
    ``max_rows + 1`` rows and failing if the file overflows.

    *declared_columns* supplies schema field names for headerless files
    (ignored for headered files, whose names come from the header).

    *sample_rows*, when given, reads at most that many rows for a deliberate
    **sample** (e.g. schema inference) and does **not** treat reaching that
    limit as an overflow error — it just truncates. The byte-size cap still
    applies (a 5 GB file is still rejected at PREFLIGHT), so a sample-infer
    works on reasonably-sized files, not arbitrarily huge ones.

    Raises a :class:`TabularReadError` subclass on any failure; the caller
    turns the error's ``code`` into a structured finding.
    """
    dialect = dialect or TabularDialect()
    limits = limits or TabularLimits()
    sampling = sample_rows is not None

    preflight = run_preflight(content, dialect=dialect, limits=limits)

    # Validate the header (if any) BEFORE the load, against the raw first
    # record — not pandas' auto-mangled columns — so a duplicate header
    # fails cleanly instead of being silently renamed to ``value.1``.
    if preflight.header_names is not None:
        canonical_header = _canonical_header_names(
            preflight.header_names,
            max_name_chars=limits.max_header_name_chars,
        )
    else:
        canonical_header = None

    try:
        frame = pd.read_csv(
            io.StringIO(preflight.text),
            sep=preflight.delimiter,
            quotechar=dialect.quotechar,
            dtype=str,
            keep_default_na=False,
            na_filter=False,
            header=0 if preflight.has_header else None,
            # Skip wholly blank lines (a trailing newline or an editor
            # artifact) rather than counting them as all-null data rows.
            # This is deliberate and load-bearing: an *empty field* (``,2``)
            # is still kept as a null cell — only a line with no content at
            # all is dropped, so ``num_rows`` reflects real data rows.
            skip_blank_lines=True,
            # In sample mode read exactly the requested rows; otherwise read
            # one past the cap so an overflow is detectable without loading an
            # unbounded number of rows.
            nrows=sample_rows if sampling else limits.max_rows + 1,
            on_bad_lines="error",
        )
    except EmptyDataError as exc:
        raise ParseError("File has no parseable rows.", code=CODE_EMPTY_FILE) from exc
    except (ParserError, ValueError) as exc:
        # Ragged rows, unbalanced quotes, and similar body-level breakage.
        msg = f"Could not parse the file as CSV: {exc}"
        raise ParseError(msg, code=CODE_PARSE_ERROR) from exc

    column_count = len(frame.columns)
    # ``column_N`` synthesis needs the post-load column count; the validated
    # header (if any) must match it, or the first-record peek and the body
    # parse disagree — a parse inconsistency we surface rather than paper over.
    if canonical_header is not None and len(canonical_header) != column_count:
        msg = (
            f"Header declares {len(canonical_header)} columns but the body "
            f"parsed {column_count}."
        )
        raise ParseError(msg, code=CODE_COLUMN_COUNT_MISMATCH)

    logical_names = _resolve_logical_names(
        column_count=column_count,
        header_names=canonical_header,
        declared_columns=declared_columns,
        max_header_name_chars=limits.max_header_name_chars,
    )
    frame.columns = pd.Index(logical_names)

    num_rows = len(frame)
    # Overflow is only an error for a full read; a sample deliberately truncates.
    if not sampling and num_rows > limits.max_rows:
        msg = f"File has more than the {limits.max_rows}-row limit."
        raise ParseError(msg, code=CODE_TOO_MANY_ROWS)

    return ReadResult(
        dataframe=frame,
        column_names=logical_names,
        num_rows=num_rows,
        num_columns=column_count,
        preflight=preflight,
    )
