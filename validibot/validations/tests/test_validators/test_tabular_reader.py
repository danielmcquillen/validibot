"""
Tests for the Tabular Validator's CSV reader and PREFLIGHT
(:mod:`validibot.validations.validators.tabular.preflight` and
:mod:`validibot.validations.validators.tabular.readers.csv`).

### What this suite covers and why

The reader is the foundation of the whole validator: every later layer
(native structured validation, row-stage CEL) consumes the string-valued
dataframe and canonical column names it produces. The behaviours pinned
here are the ones ADR-2026-05-26 calls load-bearing for determinism and
safety:

- **PREFLIGHT bounds cost before the load** — oversized/undecodable/too-wide
  files are rejected cheaply, before a dataframe is built.
- **Reads are deterministic and locale-free** — every cell is a string,
  empty cells are ``""`` (not NaN), and ``num_rows == len(df)`` counts
  parsed rows, not byte-level newlines (RFC 4180 quoted newlines).
- **Strict parsing** — ragged rows fail rather than being silently
  repaired; a corrupted file must never look valid.
- **One canonical column name** — headered files use the validated header;
  headerless files synthesise ``column_N`` unless a name is declared.
- **Unsafe headers fail by default** — duplicate / blank / case-colliding
  names, because ``row.*`` keys come from them.

These are pure functions (no Django, no DB), so the suite uses
``SimpleTestCase``. Error assertions check the structured ``code`` on the
raised :class:`TabularReadError`, not the message text, because the code
is the stable machine-readable contract the validator emits as a finding.
"""

from __future__ import annotations

import csv
from unittest.mock import patch

import pytest
from django.test import SimpleTestCase

from validibot.validations.validators.tabular.preflight import CODE_DIALECT_MISMATCH
from validibot.validations.validators.tabular.preflight import CODE_DIALECT_UNDETERMINED
from validibot.validations.validators.tabular.preflight import CODE_EMPTY_FILE
from validibot.validations.validators.tabular.preflight import CODE_ENCODING_ERROR
from validibot.validations.validators.tabular.preflight import CODE_FILE_TOO_LARGE
from validibot.validations.validators.tabular.preflight import CODE_TOO_MANY_COLUMNS
from validibot.validations.validators.tabular.preflight import PreflightError
from validibot.validations.validators.tabular.preflight import TabularDialect
from validibot.validations.validators.tabular.preflight import TabularLimits
from validibot.validations.validators.tabular.preflight import run_preflight
from validibot.validations.validators.tabular.readers.csv import CODE_BLANK_HEADER
from validibot.validations.validators.tabular.readers.csv import CODE_DUPLICATE_HEADER
from validibot.validations.validators.tabular.readers.csv import (
    CODE_HEADER_CASE_COLLISION,
)
from validibot.validations.validators.tabular.readers.csv import (
    CODE_HEADER_NAME_TOO_LONG,
)
from validibot.validations.validators.tabular.readers.csv import CODE_PARSE_ERROR
from validibot.validations.validators.tabular.readers.csv import CODE_TOO_MANY_ROWS
from validibot.validations.validators.tabular.readers.csv import ParseError
from validibot.validations.validators.tabular.readers.csv import read_csv

# ─────────────────────────────────────────────────────────────────────
# PREFLIGHT — cheap checks before the dataframe load
# ─────────────────────────────────────────────────────────────────────


class PreflightTests(SimpleTestCase):
    """The pre-load guards that keep a hostile/huge file off the worker."""

    def test_oversized_file_rejected_before_load(self):
        """A file over the byte cap is rejected by PREFLIGHT.

        This is the guard that prevents a multi-GB upload from being
        decoded or loaded at all — the cap is checked on raw bytes first.
        """
        limits = TabularLimits(max_bytes=10)
        with pytest.raises(PreflightError) as exc_info:
            run_preflight(b"a,b,c\n1,2,3\n", limits=limits)
        self.assertEqual(exc_info.value.code, CODE_FILE_TOO_LARGE)

    def test_empty_file_rejected(self):
        """A zero-byte (or whitespace-only) file is a clean failure, not a
        confusing downstream pandas error.
        """
        with pytest.raises(PreflightError) as exc_info:
            run_preflight(b"")
        self.assertEqual(exc_info.value.code, CODE_EMPTY_FILE)

    def test_undecodable_bytes_rejected(self):
        """Bytes that aren't valid UTF-8 fail with an encoding error rather
        than being lossily "replaced" — silently altering cell content
        would corrupt anything attested over the result.
        """
        with pytest.raises(PreflightError) as exc_info:
            run_preflight(b"a,b\n1,\xff\n")
        self.assertEqual(exc_info.value.code, CODE_ENCODING_ERROR)

    def test_utf8_bom_is_stripped_from_first_column(self):
        """A UTF-8 BOM must not bleed into the first column name.

        Without ``utf-8-sig`` decoding, the first header would be
        ``"\\ufeffname"`` and ``row.name`` would silently never match.
        """
        content = "﻿name,age\nAlice,30\n".encode()
        result = run_preflight(content)
        self.assertEqual(result.header_names, ["name", "age"])

    def test_declared_delimiter_overrides_and_mismatch_fails(self):
        """The declared delimiter is authoritative, but a declared/sniffed
        disagreement is a clean failure — an honest "you said comma, this
        looks tab-delimited" beats silently guessing.
        """
        tab_content = b"a\tb\tc\n1\t2\t3\n4\t5\t6\n"
        with pytest.raises(PreflightError) as exc_info:
            run_preflight(tab_content, dialect=TabularDialect(delimiter=","))
        self.assertEqual(exc_info.value.code, CODE_DIALECT_MISMATCH)

    def test_delimiter_is_sniffed_when_not_declared(self):
        """With no declared delimiter, PREFLIGHT sniffs it — the friendly
        default path. A tab-separated file is detected as tab-delimited.
        """
        result = run_preflight(b"a\tb\tc\n1\t2\t3\n4\t5\t6\n")
        self.assertEqual(result.delimiter, "\t")
        self.assertEqual(result.field_count, 3)

    def test_each_supported_delimiter_is_detected_from_content(self):
        """Comma, tab, semicolon, and pipe all work without filename hints.

        Keeping the complete supported set in one table-driven test prevents a
        future sniffing change from accidentally narrowing inference to the two
        most common dialects.
        """
        samples = {
            ",": b"a,b\n1,2\n",
            "\t": b"a\tb\n1\t2\n",
            ";": b"a;b\n1;2\n",
            "|": b"a|b\n1|2\n",
        }
        for expected, content in samples.items():
            with self.subTest(delimiter=repr(expected)):
                result = run_preflight(content)
                self.assertEqual(result.delimiter, expected)
                self.assertEqual(result.field_count, 2)

    def test_quoted_punctuation_does_not_create_false_ambiguity(self):
        """Supported delimiter characters inside quoted cells remain data.

        A comma-delimited export may legitimately contain tabs, semicolons, or
        pipes in text values; quote-aware candidate parsing must not mistake
        those characters for competing dialects.
        """
        content = b'a,b\n1,"tab\tpipe|semicolon;"\n2,"plain"\n'
        result = run_preflight(content)
        self.assertEqual(result.delimiter, ",")
        self.assertEqual(result.field_count, 2)

    def test_consistent_width_fallback_detects_tab_delimiter(self):
        """A valid TSV remains detectable when ``csv.Sniffer`` gives up.

        Real-world generated exports can defeat the heuristic sniffer because
        their values contain varied punctuation. The bounded fallback parses
        logical records with each supported delimiter and accepts the one
        unambiguous, consistent multi-column shape.
        """
        content = b"id\tscientificName\tdepth\n1\tA, alpha\t10\n2\tB|beta\t20\n"
        with patch(
            "validibot.validations.validators.tabular.preflight.csv.Sniffer.sniff",
            side_effect=csv.Error,
        ):
            result = run_preflight(content)
        self.assertEqual(result.delimiter, "\t")
        self.assertEqual(result.field_count, 3)

    def test_ambiguous_fallback_requires_explicit_delimiter(self):
        """Equally plausible delimiters fail instead of becoming one column.

        When automatic detection has no defensible answer, asking the author
        to select a delimiter is safer than silently falling back to comma and
        proposing a misleading schema.
        """
        content = b"a\tb,c\n1\t2,3\n"
        with pytest.raises(PreflightError) as exc_info:
            run_preflight(content)
        self.assertEqual(exc_info.value.code, CODE_DIALECT_UNDETERMINED)

    def test_explicit_delimiter_resolves_ambiguous_content(self):
        """The author's explicit selection resolves an automatic-detect tie.

        The ambiguity error is actionable rather than a dead end: selecting tab
        makes the same bytes parse deterministically as the intended table.
        """
        content = b"a\tb,c\n1\t2,3\n"
        result = run_preflight(
            content,
            dialect=TabularDialect(delimiter="\t"),
        )
        self.assertEqual(result.delimiter, "\t")
        self.assertEqual(result.field_count, 2)

    def test_single_column_without_delimiter_remains_valid(self):
        """No delimiter at all is a legitimate one-column table.

        Ambiguity protection must not reject a plain list merely because the
        internal reader needs a dialect value; comma remains the inert default.
        """
        result = run_preflight(b"occurrenceID\nurn:one\nurn:two\n")
        self.assertEqual(result.delimiter, ",")
        self.assertEqual(result.field_count, 1)

    def test_too_many_columns_rejected(self):
        """A file wider than the column cap is rejected in PREFLIGHT, from
        the first record alone — no need to load the body.
        """
        limits = TabularLimits(max_columns=2)
        with pytest.raises(PreflightError) as exc_info:
            run_preflight(b"a,b,c\n1,2,3\n", limits=limits)
        self.assertEqual(exc_info.value.code, CODE_TOO_MANY_COLUMNS)


# ─────────────────────────────────────────────────────────────────────
# READ — building the dataframe (headered, the common case)
# ─────────────────────────────────────────────────────────────────────


class ReadHeaderedTests(SimpleTestCase):
    """Reading a normal headered CSV into the shared in-memory model."""

    def test_basic_read_columns_rows_and_string_cells(self):
        """A headered file yields header-named columns, the right row
        count, and string cells — the baseline contract everything else
        builds on.
        """
        result = read_csv(b"name,age,city\nAlice,30,NYC\nBob,25,LA\n")
        self.assertEqual(result.column_names, ["name", "age", "city"])
        self.assertEqual(result.num_rows, 2)
        self.assertEqual(result.num_columns, 3)
        # Cells are strings, not coerced numbers — coercion is a later layer.
        self.assertEqual(result.dataframe.iloc[0]["age"], "30")
        self.assertIsInstance(result.dataframe.iloc[0]["age"], str)

    def test_empty_cell_is_empty_string_not_nan(self):
        """A missing cell is ``""``, never NaN.

        ``na_filter=False`` keeps empties distinguishable and stops pandas
        from turning a blank into a float NaN that would poison downstream
        type coercion and null semantics.
        """
        result = read_csv(b"a,b,c\n1,,3\n")
        self.assertEqual(result.dataframe.iloc[0]["b"], "")

    def test_num_rows_counts_logical_rows_not_newlines(self):
        """A quoted field with an embedded newline is one row.

        ``i.num_rows`` is ``len(df)`` — a byte-level newline count would
        over-report here (RFC 4180 allows newlines inside quoted fields).
        """
        result = read_csv(b'a,b\n1,"line1\nline2"\n2,plain\n')
        self.assertEqual(result.num_rows, 2)

    def test_header_whitespace_is_trimmed(self):
        """Leading/trailing whitespace in header names is trimmed; the
        trimmed form is canonical, so ``row.name`` works regardless of
        sloppy spacing in the file.
        """
        result = read_csv(b"  name  , age \nA,1\n")
        self.assertEqual(result.column_names, ["name", "age"])

    def test_blank_lines_are_skipped_but_empty_fields_are_kept(self):
        """A wholly blank line is dropped (so a trailing newline or stray
        blank line doesn't inflate the row count), but an empty *field* is
        kept as a null cell. This is the load-bearing distinction that keeps
        ``num_rows`` equal to the count of real data rows.
        """
        # Row 2 is a blank line (skipped); the ``,`` rows have empty fields.
        result = read_csv(b"a,b\n1,2\n\n3,\n")
        self.assertEqual(result.num_rows, 2)
        self.assertEqual(result.dataframe.iloc[1]["b"], "")


# ─────────────────────────────────────────────────────────────────────
# READ — strict parsing and the row cap
# ─────────────────────────────────────────────────────────────────────


class ReadStrictnessTests(SimpleTestCase):
    """The quality-first posture: malformed bodies fail, they aren't fixed."""

    def test_ragged_row_fails_at_read(self):
        """A row with the wrong number of fields fails parsing.

        PREFLIGHT can't see this (it only peeks the first record); strict
        parsing at READ catches it. We never silently drop or pad the row.
        """
        with pytest.raises(ParseError) as exc_info:
            read_csv(b"a,b\n1,2\n3,4,5\n")
        self.assertEqual(exc_info.value.code, CODE_PARSE_ERROR)

    def test_row_cap_overflow_fails(self):
        """Exceeding the row cap fails the read rather than silently
        truncating — a partial validation must never look like a complete
        one.
        """
        limits = TabularLimits(max_rows=2)
        content = b"a\n1\n2\n3\n"  # 3 data rows, cap is 2
        with pytest.raises(ParseError) as exc_info:
            read_csv(content, limits=limits)
        self.assertEqual(exc_info.value.code, CODE_TOO_MANY_ROWS)

    def test_row_at_cap_is_allowed(self):
        """Exactly ``max_rows`` rows must succeed — guards against an
        off-by-one that would reject legitimate files at the boundary.
        """
        limits = TabularLimits(max_rows=2)
        result = read_csv(b"a\n1\n2\n", limits=limits)
        self.assertEqual(result.num_rows, 2)

    def test_sample_rows_truncates_without_overflow_error(self):
        """``sample_rows`` reads a bounded sample and truncates rather than
        erroring — the read mode schema inference relies on. The same file
        without sampling and a low row cap would raise.
        """
        result = read_csv(b"a\n1\n2\n3\n4\n5\n", sample_rows=2)
        self.assertEqual(result.num_rows, 2)


# ─────────────────────────────────────────────────────────────────────
# READ — column-name resolution (the one naming rule)
# ─────────────────────────────────────────────────────────────────────


class ColumnNameResolutionTests(SimpleTestCase):
    """The single precedence rule that decides each column's logical name."""

    def test_headerless_without_declared_synthesises_column_n(self):
        """A headerless file with no declared names gets ``column_1..N`` so
        the ``row.*`` namespace is always populated — a headerless file is
        a first-class input, not a disabled one.
        """
        result = read_csv(
            b"1,2,3\n4,5,6\n",
            dialect=TabularDialect(has_header=False),
        )
        self.assertEqual(result.column_names, ["column_1", "column_2", "column_3"])
        self.assertEqual(result.num_rows, 2)

    def test_headerless_with_declared_uses_declared_then_defaults(self):
        """Declared names win positionally for a headerless file; any
        position past the declared list falls back to ``column_N``.

        This is the precedence rule: ``column_N`` is the *default*, not a
        competing identity — a declared name replaces it.
        """
        result = read_csv(
            b"1,2,3\n4,5,6\n",
            dialect=TabularDialect(has_header=False),
            declared_columns=["id", "x"],
        )
        self.assertEqual(result.column_names, ["id", "x", "column_3"])

    def test_headered_ignores_declared_columns(self):
        """For a headered file the header wins; declared names do not rename
        columns (Table Schema requires a field name to equal its header).
        """
        result = read_csv(
            b"name,age\nA,1\n",
            declared_columns=["renamed_a", "renamed_b"],
        )
        self.assertEqual(result.column_names, ["name", "age"])


# ─────────────────────────────────────────────────────────────────────
# READ — unsafe headers fail by default (row.* key safety)
# ─────────────────────────────────────────────────────────────────────


class UnsafeHeaderTests(SimpleTestCase):
    """Headers that would make ``row.*`` ambiguous or unaddressable fail."""

    def test_duplicate_header_fails(self):
        """Two columns named ``value`` make ``row.value`` ambiguous, so a
        duplicate header is rejected rather than silently renamed.
        """
        with pytest.raises(ParseError) as exc_info:
            read_csv(b"value,value\n1,2\n")
        self.assertEqual(exc_info.value.code, CODE_DUPLICATE_HEADER)

    def test_blank_header_fails(self):
        """An unnamed column can't be addressed by ``row.*``; a blank header
        name is rejected.
        """
        with pytest.raises(ParseError) as exc_info:
            read_csv(b"a,,c\n1,2,3\n")
        self.assertEqual(exc_info.value.code, CODE_BLANK_HEADER)

    def test_case_only_collision_fails(self):
        """``Lat`` and ``lat`` collide under Table Schema's case-insensitive
        uniqueness rule, so they're rejected rather than silently picking
        one.
        """
        with pytest.raises(ParseError) as exc_info:
            read_csv(b"Lat,lat\n1,2\n")
        self.assertEqual(exc_info.value.code, CODE_HEADER_CASE_COLLISION)

    def test_overlong_header_name_fails_before_schema_generation(self):
        """A huge header cell cannot inflate an inferred or stored schema.

        The test uses a deliberately small custom limit to exercise the same
        parser guard without constructing a large fixture.
        """
        limits = TabularLimits(max_header_name_chars=8)
        with pytest.raises(ParseError) as exc_info:
            read_csv(b"occurrenceID,value\nurn:one,1\n", limits=limits)
        self.assertEqual(exc_info.value.code, CODE_HEADER_NAME_TOO_LONG)
