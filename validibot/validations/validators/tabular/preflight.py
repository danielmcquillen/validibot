"""PREFLIGHT — cheap, pre-load checks for the Tabular Validator's reader.

PREFLIGHT runs *before* the (potentially large) dataframe load. It
inspects only what is cheap to inspect without parsing the whole file:

- **byte size** — reject an oversized file before decoding or loading it;
- **encoding / BOM** — decode the bytes (BOM-aware) or fail cleanly;
- **dialect** — the delimiter, declared or sniffed; and
- **the FIRST RECORD only** — the header row for headered files, or the
  first data record for headerless files — to derive the column
  names / field count.

It deliberately does **not** see the body. Ragged rows, unbalanced
quotes, and row-count overflow are caught later at READ, where strict
parsing fails — PREFLIGHT only bounds the cost of reaching that failure.
See ADR-2026-05-26 (Tabular Validator), "Evaluation pipeline".

Everything here is pure (no Django, no models), so it is unit-testable in
isolation and shared by every future reader (CSV in V1; TSV/Excel/Parquet
later).
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

# ── Finding/error codes ────────────────────────────────────────────────
# All tabular finding codes are prefixed ``tabular.`` (never ``csv.``) per
# the ADR invariants block. PREFLIGHT raises these as TabularReadError;
# the validator (a later slice) turns them into ValidationIssues.
CODE_FILE_TOO_LARGE = "tabular.file_too_large"
CODE_ENCODING_ERROR = "tabular.encoding_error"
CODE_EMPTY_FILE = "tabular.empty_file"
CODE_TOO_MANY_COLUMNS = "tabular.too_many_columns"
CODE_DIALECT_MISMATCH = "tabular.dialect_mismatch"

# Sniff the dialect from at most this many decoded characters. The header
# and a few rows are plenty to detect a delimiter; sniffing the whole file
# would defeat the point of a cheap PREFLIGHT.
_SNIFF_SAMPLE_CHARS = 65536

# Candidate delimiters offered to ``csv.Sniffer``. Constraining the set
# avoids the sniffer guessing an exotic separator from incidental
# punctuation in the data.
_SNIFF_DELIMITERS = ",\t;|"

# Default delimiter when nothing is declared and sniffing is inconclusive
# (e.g. a single-column file has no delimiter to detect). Comma is the
# overwhelmingly common case and the format the V1 reader is named for.
_DEFAULT_DELIMITER = ","


@dataclass(frozen=True)
class TabularDialect:
    """File-format settings for reading a table (a subset of CSV Dialect).

    ``delimiter=None`` means "sniff it"; an explicit value overrides the
    sniff (and a declared/sniffed disagreement fails — see the delimiter
    decision in the ADR). ``has_header=False`` marks a headerless file,
    whose columns get synthesised positional names at READ time.
    """

    delimiter: str | None = None
    quotechar: str = '"'
    encoding: str = "utf-8"
    has_header: bool = True


@dataclass(frozen=True)
class TabularLimits:
    """Enforceable caps that make "human-scale, in-memory" a real contract.

    Deployment-tunable (a self-hosted operator can raise them) with safe
    defaults so one large or adversarial submission cannot exhaust the
    shared worker. ``max_bytes``/``max_columns`` are enforced here in
    PREFLIGHT (no full load); ``max_rows`` is enforced at READ.
    """

    max_bytes: int = 50 * 1024 * 1024  # 50 MB
    max_rows: int = 1_000_000
    max_columns: int = 1024


@dataclass(frozen=True)
class PreflightResult:
    """Outcome of PREFLIGHT — everything the reader needs to load the table.

    Carries the decoded ``text`` so the reader does not decode twice (the
    byte-size cap already bounded the decode). ``header_names`` is the raw
    first-row strings when ``has_header`` is true (the reader validates and
    canonicalises them); it is ``None`` for headerless files.
    """

    size_bytes: int
    encoding: str
    delimiter: str
    has_header: bool
    field_count: int
    header_names: list[str] | None
    text: str


class TabularReadError(Exception):
    """Base error for tabular PREFLIGHT/READ failures.

    Carries a machine-readable ``code`` (a ``tabular.*`` string) alongside
    the human-readable message so the validator can emit a structured
    finding without string-matching the message text.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


class PreflightError(TabularReadError):
    """A failure detected during PREFLIGHT (before the dataframe load).

    Separate from the READ-time parse error so callers can distinguish
    "rejected cheaply, before loading" from "failed while parsing the
    body" — the two have different cost and different meaning.
    """


def _decode(content: bytes, encoding: str) -> str:
    """Decode *content* using *encoding*, stripping a UTF-8 BOM if present.

    UTF-8 is decoded as ``utf-8-sig`` so a leading byte-order mark is
    removed from the first cell before the header is read (a BOM left in
    place would corrupt the first column name). Decoding is strict: an
    undecodable byte sequence is a clean PREFLIGHT failure, never a lossy
    "replace" that would silently alter the data being attested over.
    """
    codec = "utf-8-sig" if encoding.lower() in {"utf-8", "utf8"} else encoding
    try:
        return content.decode(codec)
    except (UnicodeDecodeError, LookupError) as exc:
        msg = f"Could not decode the file as {encoding!r}: {exc}"
        raise PreflightError(msg, code=CODE_ENCODING_ERROR) from exc


def _sniff_delimiter(sample: str) -> str | None:
    """Best-effort delimiter detection over a bounded text sample.

    Returns ``None`` when the sniffer cannot decide (e.g. a single-column
    file with no delimiter at all) so the caller can fall back to a
    sensible default rather than propagating a sniff error.
    """
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=_SNIFF_DELIMITERS)
    except csv.Error:
        return None
    return dialect.delimiter


def _resolve_delimiter(declared: str | None, sample: str) -> str:
    """Apply the delimiter decision: declared overrides, mismatch fails.

    - If a delimiter is declared, it is authoritative; but if a sniff also
      produces a *different* delimiter, the disagreement is a clean failure
      (an honest "you said comma, this looks tab-delimited" beats a guess).
    - If nothing is declared, use the sniffed delimiter; if sniffing is
      inconclusive, fall back to the default (comma).
    """
    sniffed = _sniff_delimiter(sample)
    if declared is not None:
        if sniffed is not None and sniffed != declared:
            msg = (
                f"Declared delimiter {declared!r} does not match the "
                f"detected delimiter {sniffed!r}."
            )
            raise PreflightError(msg, code=CODE_DIALECT_MISMATCH)
        return declared
    return sniffed if sniffed is not None else _DEFAULT_DELIMITER


def _read_first_record(text: str, delimiter: str, quotechar: str) -> list[str]:
    """Return the fields of the first record, honouring quoting.

    Used to derive the column count (and, for headered files, the raw
    header names) without parsing the whole file. Uses the ``csv`` module
    so a quoted field containing the delimiter or an embedded newline is
    counted as one field, not split.
    """
    reader = csv.reader(io.StringIO(text), delimiter=delimiter, quotechar=quotechar)
    for record in reader:
        return record
    return []


def run_preflight(
    content: bytes,
    *,
    dialect: TabularDialect | None = None,
    limits: TabularLimits | None = None,
) -> PreflightResult:
    """Run the cheap pre-load checks and return what the reader needs.

    Order matters: the byte-size cap is checked on the raw bytes *first*,
    so an oversized file is rejected before it is even decoded — that is
    the guard which keeps a 5 GB upload from exhausting the worker. Only
    after that do we decode, sniff the dialect, and peek at the first
    record.

    Raises :class:`PreflightError` (a :class:`TabularReadError`) on any
    cheap, pre-load problem: oversized file, undecodable encoding, empty
    file, or too many columns.
    """
    dialect = dialect or TabularDialect()
    limits = limits or TabularLimits()

    size_bytes = len(content)
    if size_bytes > limits.max_bytes:
        msg = f"File is {size_bytes} bytes, over the {limits.max_bytes}-byte limit."
        raise PreflightError(msg, code=CODE_FILE_TOO_LARGE)
    if size_bytes == 0:
        raise PreflightError("File is empty.", code=CODE_EMPTY_FILE)

    text = _decode(content, dialect.encoding)
    if not text.strip():
        raise PreflightError("File has no content.", code=CODE_EMPTY_FILE)

    sample = text[:_SNIFF_SAMPLE_CHARS]
    delimiter = _resolve_delimiter(dialect.delimiter, sample)

    first_record = _read_first_record(text, delimiter, dialect.quotechar)
    field_count = len(first_record)
    if field_count > limits.max_columns:
        msg = (
            f"File has {field_count} columns, over the "
            f"{limits.max_columns}-column limit."
        )
        raise PreflightError(msg, code=CODE_TOO_MANY_COLUMNS)

    return PreflightResult(
        size_bytes=size_bytes,
        encoding=dialect.encoding,
        delimiter=delimiter,
        has_header=dialect.has_header,
        field_count=field_count,
        header_names=list(first_record) if dialect.has_header else None,
        text=text,
    )
