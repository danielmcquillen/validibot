"""
Tests for the Tabular Validator's schema parser, cell coercion, and native
structured validation (``validators/tabular/schema.py``, ``coercion.py``,
``native.py``).

### What this suite covers and why

Native validation is the non-CEL lane: the structured constraints a non-coder
declares in the settings, checked directly against the dataframe. The
behaviours pinned here are the ones the ADR makes load-bearing:

- **Schema parsing** adopts Table Schema's vocabulary without depending on the
  ``frictionless`` library; unknown types degrade to ``string`` and a malformed
  descriptor fails loudly.
- **Coercion is deterministic and locale-free** — empty is null, ``"1,000"``
  is not a number, ``"5.0"`` is not an integer, dates are ISO 8601 only.
- **Findings aggregate per check** — one finding per (column, check) with a
  count and capped, 1-based sample rows, never one per failing row.
- **Uniqueness null semantics** — ``unique`` exempts nulls (SQL), ``primaryKey``
  forbids them, and comparison is on canonical typed values.

These are pure functions (no DB), so the suite uses ``SimpleTestCase`` and
builds inputs through the real ``read_csv`` reader.
"""

from __future__ import annotations

import time

import pytest
from django.test import SimpleTestCase

from validibot.validations.validators.tabular.coercion import coerce_cell
from validibot.validations.validators.tabular.native import (
    CODE_CONDITIONAL_REQUIRED_COLUMN,
)
from validibot.validations.validators.tabular.native import CODE_ENUM_VIOLATION
from validibot.validations.validators.tabular.native import CODE_INVALID_PATTERN
from validibot.validations.validators.tabular.native import CODE_LENGTH_ERROR
from validibot.validations.validators.tabular.native import CODE_MISSING_REQUIRED_COLUMN
from validibot.validations.validators.tabular.native import CODE_OUT_OF_RANGE
from validibot.validations.validators.tabular.native import CODE_PATTERN_MISMATCH
from validibot.validations.validators.tabular.native import CODE_PRIMARY_KEY_NULL
from validibot.validations.validators.tabular.native import CODE_REQUIRED_VALUE_MISSING
from validibot.validations.validators.tabular.native import CODE_TIMED_OUT
from validibot.validations.validators.tabular.native import CODE_TYPE_ERROR
from validibot.validations.validators.tabular.native import CODE_UNIQUE_VIOLATION
from validibot.validations.validators.tabular.native import DEFAULT_REPORT_MAX_EXAMPLES
from validibot.validations.validators.tabular.native import _validate_pattern
from validibot.validations.validators.tabular.native import validate_native
from validibot.validations.validators.tabular.readers.csv import read_csv
from validibot.validations.validators.tabular.schema import parse_table_schema


def _codes(findings):
    """Collect the set of finding codes for concise assertions."""
    return {finding.code for finding in findings}


def _by_code(findings):
    """Index findings by code (codes are unique per validate_native call)."""
    return {finding.code: finding for finding in findings}


# ─────────────────────────────────────────────────────────────────────
# Schema parsing
# ─────────────────────────────────────────────────────────────────────


class TableSchemaParseTests(SimpleTestCase):
    """Parsing a Frictionless descriptor into the internal schema model."""

    def test_parses_fields_types_constraints_and_primary_key(self):
        """A full descriptor round-trips into typed fields, constraints, and a
        composite primary key — the shape descriptor import will produce.
        """
        schema = parse_table_schema(
            {
                "fields": [
                    {
                        "name": "lat",
                        "type": "number",
                        "constraints": {
                            "required": True,
                            "minimum": -90,
                            "maximum": 90,
                        },
                    },
                    {"name": "id", "type": "string", "constraints": {"unique": True}},
                ],
                "primaryKey": ["lat", "id"],
            },
        )
        self.assertEqual(schema.field_names(), ["lat", "id"])
        self.assertEqual(schema.primary_key, ("lat", "id"))
        self.assertTrue(schema.fields[0].constraints.required)
        self.assertEqual(schema.fields[0].constraints.minimum, -90.0)
        self.assertTrue(schema.fields[1].constraints.unique)

    def test_parses_validibot_conditional_requiredness_extension(self):
        """The V2 no-CEL widget round-trips through the schema model.

        The extension is deliberately narrow: one declared column becomes
        required when another declared column is present in the submitted file.
        """
        schema = parse_table_schema(
            {
                "fields": [
                    {"name": "measurementType"},
                    {
                        "name": "measurementTypeID",
                        "constraints": {
                            "x-validibot-requiredWhenPresent": "measurementType",
                        },
                    },
                ],
            },
        )
        self.assertEqual(
            schema.fields[1].constraints.required_when_present,
            "measurementType",
        )

    def test_conditional_requiredness_rejects_unknown_trigger(self):
        """A descriptor cannot depend on a column it does not declare."""
        with pytest.raises(ValueError, match="unknown conditional-requiredness"):
            parse_table_schema(
                {
                    "fields": [
                        {
                            "name": "measurementTypeID",
                            "constraints": {
                                "x-validibot-requiredWhenPresent": "missing",
                            },
                        },
                    ],
                },
            )

    def test_string_primary_key_is_normalised_to_tuple(self):
        """A scalar ``primaryKey`` string becomes a one-element tuple, so the
        single- and composite-key code paths are uniform.
        """
        schema = parse_table_schema(
            {"fields": [{"name": "id"}], "primaryKey": "id"},
        )
        self.assertEqual(schema.primary_key, ("id",))

    def test_unknown_type_falls_back_to_string(self):
        """An exotic/unsupported type degrades to ``string`` so an imported
        descriptor still loads rather than erroring on a type we don't model.
        """
        schema = parse_table_schema(
            {"fields": [{"name": "loc", "type": "geopoint"}]},
        )
        self.assertEqual(schema.fields[0].type, "string")

    def test_nameless_field_is_skipped(self):
        """A field with no name can't be addressed, so it's dropped rather than
        producing an unusable column spec.
        """
        schema = parse_table_schema(
            {"fields": [{"type": "string"}, {"name": "ok"}]},
        )
        self.assertEqual(schema.field_names(), ["ok"])

    def test_malformed_descriptor_raises(self):
        """A wrong-type descriptor raises ``TypeError``; a right-type but
        unusable one (no fields) raises ``ValueError``. Either way it fails
        loudly at configuration time — silently validating nothing is worse.
        """
        with pytest.raises(TypeError, match="JSON object"):
            parse_table_schema(["not", "a", "dict"])
        with pytest.raises(TypeError, match="fields"):
            parse_table_schema({"primaryKey": "id"})
        with pytest.raises(ValueError, match="no usable fields"):
            parse_table_schema({"fields": [{"type": "string"}]})

    # ── Field-name uniqueness (P1 regression) ───────────────────────────
    # Why this matters: for a headerless file the schema field names become
    # the dataframe's column labels. A duplicate label makes ``frame[name]``
    # return a DataFrame instead of a Series, so native validation's
    # ``frame[field.name].tolist()`` would raise ``AttributeError`` mid-run.
    # parse_table_schema must reject the bad descriptor up front (mirroring
    # header validation) so the crash becomes a clean configuration error.

    def test_duplicate_field_names_rejected(self):
        """Two fields named identically are rejected at parse time.

        Without this, a headerless validation against the schema crashes on a
        pandas DataFrame-vs-Series ``.tolist()`` — see the section comment.
        """
        with pytest.raises(ValueError, match="duplicate field name"):
            parse_table_schema(
                {"fields": [{"name": "lat"}, {"name": "lat"}]},
            )

    def test_blank_after_trim_field_name_rejected(self):
        """A present-but-blank name (empty or whitespace-only) is rejected.

        A declared column with no usable name can't be addressed by ``row.*``
        or by native validation, so it's a loud error rather than a silent skip
        (unlike a field with no ``name`` key at all, which is genuinely
        nameless and dropped).
        """
        with pytest.raises(ValueError, match="blank field name"):
            parse_table_schema({"fields": [{"name": "   "}, {"name": "ok"}]})

    def test_case_only_collision_field_names_rejected(self):
        """``Lat`` and ``lat`` collide: Table Schema treats names as
        not-case-sensitive for uniqueness, so this is one ambiguous column,
        not two — rejected to match header validation.
        """
        with pytest.raises(ValueError, match="collide ignoring case"):
            parse_table_schema(
                {"fields": [{"name": "Lat"}, {"name": "lat"}]},
            )


# ─────────────────────────────────────────────────────────────────────
# Cell coercion
# ─────────────────────────────────────────────────────────────────────


class CoerceCellTests(SimpleTestCase):
    """Deterministic, locale-free coercion of a raw string cell."""

    def test_empty_is_null_for_any_type(self):
        """An empty cell is null regardless of type — nullability is decided by
        the ``required`` constraint, not by coercion.
        """
        for field_type in ("string", "number", "integer", "boolean", "date"):
            with self.subTest(field_type=field_type):
                result = coerce_cell("", field_type)
                self.assertTrue(result.is_null)
                self.assertTrue(result.ok)

    def test_number_rejects_locale_grouping(self):
        """``"1,000"`` is a type error, not 1000 — we never apply locale
        thousands-grouping, which would make results locale-dependent.
        """
        self.assertTrue(coerce_cell("1.5", "number").ok)
        self.assertEqual(coerce_cell("1.5", "number").value, 1.5)
        self.assertFalse(coerce_cell("1,000", "number").ok)
        self.assertFalse(coerce_cell("abc", "number").ok)

    def test_integer_rejects_float_strings(self):
        """``"5"`` is an integer but ``"5.0"`` and ``"5.5"`` are not — an
        integer column must hold whole-number literals.
        """
        self.assertEqual(coerce_cell("5", "integer").value, 5)
        self.assertFalse(coerce_cell("5.0", "integer").ok)
        self.assertFalse(coerce_cell("5.5", "integer").ok)

    def test_boolean_accepts_fixed_spellings(self):
        """A fixed true/false spelling set keeps boolean coercion predictable;
        anything else (``"yes"``) is a type error.
        """
        self.assertTrue(coerce_cell("true", "boolean").value is True)
        self.assertTrue(coerce_cell("0", "boolean").value is False)
        self.assertFalse(coerce_cell("yes", "boolean").ok)

    def test_date_accepts_iso8601_only(self):
        """ISO 8601 parses to a tz-aware datetime; a non-ISO date is a type
        error so the determinism contract holds across locales.
        """
        ok = coerce_cell("2020-01-02", "date")
        self.assertTrue(ok.ok)
        self.assertEqual(ok.value.year, 2020)
        self.assertFalse(coerce_cell("01/02/2020", "date").ok)


# ─────────────────────────────────────────────────────────────────────
# Native validation — structural and per-column checks
# ─────────────────────────────────────────────────────────────────────


class NativeColumnChecksTests(SimpleTestCase):
    """Required columns, nullability, type, and value constraints."""

    def test_valid_data_produces_no_findings(self):
        """A file that satisfies every constraint yields zero findings — the
        baseline that proves the checks don't false-positive.
        """
        read_result = read_csv(b"lat,lon\n10,20\n-5,30\n")
        schema = parse_table_schema(
            {
                "fields": [
                    {
                        "name": "lat",
                        "type": "number",
                        "constraints": {"minimum": -90, "maximum": 90},
                    },
                    {"name": "lon", "type": "number"},
                ],
            },
        )
        self.assertEqual(validate_native(read_result, schema), [])

    def test_missing_required_column_is_reported_once(self):
        """A required column absent from the file is a single structural
        finding — not one per row.
        """
        read_result = read_csv(b"lon\n20\n")
        schema = parse_table_schema(
            {"fields": [{"name": "lat", "constraints": {"required": True}}]},
        )
        findings = validate_native(read_result, schema)
        self.assertIn(CODE_MISSING_REQUIRED_COLUMN, _codes(findings))
        self.assertEqual(_by_code(findings)[CODE_MISSING_REQUIRED_COLUMN].column, "lat")

    def test_absent_optional_column_is_skipped(self):
        """An optional column that isn't in the file produces no finding — it's
        simply not validated, not flagged missing.
        """
        read_result = read_csv(b"lon\n20\n")
        schema = parse_table_schema({"fields": [{"name": "lat"}]})
        self.assertEqual(validate_native(read_result, schema), [])

    def test_conditional_required_column_depends_on_companion_presence(self):
        """The structured V2 condition fires only when its trigger exists."""
        schema = parse_table_schema(
            {
                "fields": [
                    {"name": "measurementType"},
                    {
                        "name": "measurementTypeID",
                        "constraints": {
                            "x-validibot-requiredWhenPresent": "measurementType",
                        },
                    },
                ],
            },
        )

        triggered = validate_native(
            read_csv(b"measurementType\nlength\n"),
            schema,
        )
        not_triggered = validate_native(read_csv(b"other\nvalue\n"), schema)

        self.assertIn(CODE_CONDITIONAL_REQUIRED_COLUMN, _codes(triggered))
        self.assertNotIn(CODE_CONDITIONAL_REQUIRED_COLUMN, _codes(not_triggered))

    def test_required_value_missing_counts_null_cells(self):
        """An empty *field* in a required column is a nullability violation,
        with a count and 1-based sample rows (file order).

        Note we use an empty field (``2,``), not a blank line: blank lines are
        skipped by the reader, so they could never represent a missing value.
        """
        read_result = read_csv(b"id,name\n1,Alice\n2,\n3,Bob\n")  # row 2 name empty
        schema = parse_table_schema(
            {"fields": [{"name": "name", "constraints": {"required": True}}]},
        )
        finding = _by_code(validate_native(read_result, schema))[
            CODE_REQUIRED_VALUE_MISSING
        ]
        self.assertEqual(finding.count, 1)
        self.assertEqual(finding.sample_rows, (2,))

    def test_type_error_reported_with_samples(self):
        """Non-coercible cells in a typed column are one aggregated type-error
        finding pointing at the offending rows.
        """
        read_result = read_csv(b"age\n30\nabc\n40\n")
        schema = parse_table_schema({"fields": [{"name": "age", "type": "integer"}]})
        finding = _by_code(validate_native(read_result, schema))[CODE_TYPE_ERROR]
        self.assertEqual(finding.count, 1)
        self.assertEqual(finding.sample_rows, (2,))

    def test_numeric_out_of_range(self):
        """A value past the declared min/max is an out-of-range finding; valid
        rows are not flagged.
        """
        read_result = read_csv(b"lat\n10\n200\n-95\n")
        schema = parse_table_schema(
            {
                "fields": [
                    {
                        "name": "lat",
                        "type": "number",
                        "constraints": {"minimum": -90, "maximum": 90},
                    }
                ]
            },
        )
        finding = _by_code(validate_native(read_result, schema))[CODE_OUT_OF_RANGE]
        self.assertEqual(finding.count, 2)
        self.assertEqual(finding.sample_rows, (2, 3))

    def test_string_length_bounds(self):
        """``minLength``/``maxLength`` apply to the raw string length."""
        read_result = read_csv(b"code\nAB\nABCDE\nABC\n")
        schema = parse_table_schema(
            {
                "fields": [
                    {"name": "code", "constraints": {"minLength": 3, "maxLength": 3}}
                ]
            },
        )
        finding = _by_code(validate_native(read_result, schema))[CODE_LENGTH_ERROR]
        self.assertEqual(finding.sample_rows, (1, 2))

    def test_regex_pattern_full_match(self):
        """A pattern is a full match — a value that only partially matches is a
        mismatch (Table Schema anchors the pattern).
        """
        read_result = read_csv(b"sku\nA-1\nA-1-x\nB-2\n")
        schema = parse_table_schema(
            {"fields": [{"name": "sku", "constraints": {"pattern": r"[A-Z]-\d"}}]},
        )
        finding = _by_code(validate_native(read_result, schema))[CODE_PATTERN_MISMATCH]
        self.assertEqual(finding.sample_rows, (2,))

    def test_catastrophic_pattern_does_not_hang(self):
        """A backtracking-bomb pattern is matched in linear time, not forever.

        ``pattern`` is matched with RE2, so even ``(a+)+$`` against a crafted
        value resolves immediately rather than pinning the worker. The value
        does not match, so it is reported as a normal mismatch — the point is
        that we *get here at all* (under :mod:`re` this would never return).
        """
        read_result = read_csv(("col\n" + "a" * 80 + "!\n").encode())
        schema = parse_table_schema(
            {"fields": [{"name": "col", "constraints": {"pattern": r"(a+)+$"}}]},
        )
        finding = _by_code(validate_native(read_result, schema))[CODE_PATTERN_MISMATCH]
        self.assertEqual(finding.sample_rows, (1,))

    def test_unsupported_pattern_is_reported_as_invalid(self):
        """A pattern RE2 cannot compile is a config error, not a silent skip.

        RE2 omits lookaround, so a lookahead pattern is rejected. The validator
        must surface that as one ``tabular.invalid_pattern`` finding rather than
        falling back to the unsafe :mod:`re` engine or skipping the check (which
        would let bad data through unnoticed).
        """
        read_result = read_csv(b"col\nx\n")
        schema = parse_table_schema(
            {"fields": [{"name": "col", "constraints": {"pattern": r"foo(?=bar)"}}]},
        )
        codes = _codes(validate_native(read_result, schema))
        self.assertIn(CODE_INVALID_PATTERN, codes)
        self.assertNotIn(CODE_PATTERN_MISMATCH, codes)

    def test_enum_membership(self):
        """A value outside the enum set is flagged; allowed values pass."""
        read_result = read_csv(b"status\npresent\nabsent\nmaybe\n")
        schema = parse_table_schema(
            {
                "fields": [
                    {"name": "status", "constraints": {"enum": ["present", "absent"]}}
                ]
            },
        )
        finding = _by_code(validate_native(read_result, schema))[CODE_ENUM_VIOLATION]
        self.assertEqual(finding.sample_rows, (3,))

    def test_sample_rows_are_capped(self):
        """The sample list is capped at ``report_max_examples`` even though the
        count reflects every failure — so a bulk failure doesn't flood output.
        """
        read_result = read_csv(b"n\n" + b"x\n" * 50)  # 50 type errors
        schema = parse_table_schema({"fields": [{"name": "n", "type": "integer"}]})
        finding = _by_code(validate_native(read_result, schema, report_max_examples=5))[
            CODE_TYPE_ERROR
        ]
        self.assertEqual(finding.count, 50)
        self.assertEqual(len(finding.sample_rows), 5)

    def test_default_cap_is_10_and_still_counts_all_failures(self):
        """Without an explicit cap, a finding lists up to 10 example rows while
        ``count`` reports the true total.

        This pins the ADR default (``DEFAULT_REPORT_MAX_EXAMPLES``). Ten is
        enough context for a human to spot a pattern, but the full ``count`` is
        what tells them how big the problem really is — and the gap between the
        two is exactly what drives the "showing first 10 of N" truncation
        marker in the UI/API.
        """
        read_result = read_csv(b"n\n" + b"x\n" * 150)  # 150 type errors
        schema = parse_table_schema({"fields": [{"name": "n", "type": "integer"}]})
        # No report_max_examples passed -> the default applies.
        finding = _by_code(validate_native(read_result, schema))[CODE_TYPE_ERROR]
        self.assertEqual(finding.count, 150)
        self.assertEqual(len(finding.sample_rows), 10)
        # The examples are the FIRST 10 rows in file order, not an arbitrary
        # slice — so "rows 1, 2, 3 …" is meaningful to the reader.
        self.assertEqual(finding.sample_rows[0], 1)
        self.assertEqual(finding.sample_rows[-1], 10)


# ─────────────────────────────────────────────────────────────────────
# Native validation — uniqueness (unique / primaryKey)
# ─────────────────────────────────────────────────────────────────────


class NativeUniquenessTests(SimpleTestCase):
    """The native cross-row uniqueness checks and their null semantics."""

    def test_single_unique_flags_non_null_duplicates(self):
        """Repeated non-null values violate ``unique``; the finding lists every
        row in the duplicate group.
        """
        read_result = read_csv(b"id\nA\nB\nA\n")
        schema = parse_table_schema(
            {"fields": [{"name": "id", "constraints": {"unique": True}}]},
        )
        finding = _by_code(validate_native(read_result, schema))[CODE_UNIQUE_VIOLATION]
        self.assertEqual(set(finding.sample_rows), {1, 3})

    def test_unique_exempts_nulls(self):
        """Multiple empty cells do NOT collide under ``unique`` (SQL semantics)
        — "missing" is not a value, so two missings aren't "the same value".
        """
        # Empty fields (``,2``), not blank lines: rows 2 and 3 have empty ids.
        read_result = read_csv(b"id,v\nA,1\n,2\n,3\n")
        schema = parse_table_schema(
            {"fields": [{"name": "id", "constraints": {"unique": True}}]},
        )
        self.assertNotIn(
            CODE_UNIQUE_VIOLATION, _codes(validate_native(read_result, schema))
        )

    def test_unique_compares_canonical_typed_values(self):
        """``"1"`` and ``"1.0"`` are the same key in a numeric column, so they
        collide under ``unique`` — comparison is on coerced values, not bytes.
        """
        read_result = read_csv(b"v\n1\n1.0\n")
        schema = parse_table_schema(
            {
                "fields": [
                    {"name": "v", "type": "number", "constraints": {"unique": True}}
                ]
            },
        )
        self.assertIn(
            CODE_UNIQUE_VIOLATION, _codes(validate_native(read_result, schema))
        )

    def test_primary_key_forbids_nulls(self):
        """A null in any primary-key component is its own violation, separate
        from duplicate detection.
        """
        # Empty field in the key column on row 2 (``,2``), not a blank line.
        read_result = read_csv(b"k,v\nA,1\n,2\nB,3\n")
        schema = parse_table_schema(
            {"fields": [{"name": "k"}], "primaryKey": "k"},
        )
        finding = _by_code(validate_native(read_result, schema))[CODE_PRIMARY_KEY_NULL]
        self.assertEqual(finding.sample_rows, (2,))

    def test_composite_primary_key_uniqueness(self):
        """A composite key is keyed on the tuple of typed values: a row
        duplicates only when *all* key columns match.
        """
        # (meter, ts): rows 1 and 3 share (m1, t1); row 2 differs.
        read_result = read_csv(b"meter,ts\nm1,t1\nm1,t2\nm1,t1\n")
        schema = parse_table_schema(
            {
                "fields": [{"name": "meter"}, {"name": "ts"}],
                "primaryKey": ["meter", "ts"],
            },
        )
        finding = _by_code(validate_native(read_result, schema))[CODE_UNIQUE_VIOLATION]
        self.assertEqual(set(finding.sample_rows), {1, 3})

    def test_primary_key_missing_column_reported_once(self):
        """If a primary-key column is absent, it's reported once as a missing
        required column and the uniqueness check is skipped (not crashed).
        """
        read_result = read_csv(b"meter\nm1\n")
        schema = parse_table_schema(
            {"fields": [{"name": "meter"}], "primaryKey": ["meter", "ts"]},
        )
        codes = _codes(validate_native(read_result, schema))
        self.assertIn(CODE_MISSING_REQUIRED_COLUMN, codes)
        self.assertNotIn(CODE_PRIMARY_KEY_NULL, codes)


# ─────────────────────────────────────────────────────────────────────
# Wall-clock budget — the native lane's defence against a pathological regex
#
# A column ``pattern`` is the one native check that runs an author-supplied
# regex against every submitter-supplied cell (up to ``max_rows`` of them). A
# catastrophic-backtracking pattern could otherwise pin a shared worker, so the
# native pass carries the same wall-clock budget the row-stage CEL loop uses.
# These tests pin that the budget is honoured and fails *closed* (a timeout is a
# finding, never a silent partial pass).
# ─────────────────────────────────────────────────────────────────────


class NativeWallClockBudgetTests(SimpleTestCase):
    """Native pattern matching is wall-clock bounded and fails closed."""

    def test_pattern_scan_abandons_partial_result_past_deadline(self):
        """A scan whose deadline has already passed returns no findings.

        The deadline is checked at the first cell (index 0), so an exhausted
        budget is caught *before* a potentially catastrophic match runs. The
        scan yields nothing rather than a misleading mismatch count computed from
        the handful of rows it managed to see; ``validate_native`` surfaces the
        timeout once instead.
        """
        schema = parse_table_schema(
            {"fields": [{"name": "sku", "constraints": {"pattern": r"\d+"}}]},
        )
        field = schema.fields[0]
        # None of these match ``\d+`` — with a live budget each is a mismatch.
        valid = [(index, f"x{index}", f"x{index}") for index in range(3)]

        findings = _validate_pattern(
            field,
            valid,
            r"\d+",
            DEFAULT_REPORT_MAX_EXAMPLES,
            deadline=time.monotonic() - 1.0,
        )

        self.assertEqual(findings, [])

    def test_exhausted_budget_reports_timeout_not_a_partial_verdict(self):
        """A spent budget skips the remaining checks and emits one timeout.

        A negative budget puts the deadline in the past, so the per-column loop
        stops before validating ``sku`` (whose pattern every row would otherwise
        fail). The verdict is a single ``tabular.timed_out`` finding — never a
        pattern-mismatch conclusion drawn from a fraction of the file, which
        would be a misleading (and exploitable) silent truncation.
        """
        read_result = read_csv(b"sku\nx\ny\nz\n")
        schema = parse_table_schema(
            {"fields": [{"name": "sku", "constraints": {"pattern": r"\d+"}}]},
        )

        codes = _codes(
            validate_native(read_result, schema, wall_clock_budget_s=-1.0),
        )

        self.assertIn(CODE_TIMED_OUT, codes)
        self.assertNotIn(CODE_PATTERN_MISMATCH, codes)

    def test_pattern_within_budget_validates_normally(self):
        """A generous budget leaves normal pattern validation unchanged.

        The budget is a safety valve, not a behaviour change: with time to spare,
        a non-matching value is still reported as a pattern mismatch and no
        timeout finding appears. This guards against the budget accidentally
        short-circuiting ordinary runs.
        """
        read_result = read_csv(b"sku\nA-1\nbad\n")
        schema = parse_table_schema(
            {"fields": [{"name": "sku", "constraints": {"pattern": r"[A-Z]-\d"}}]},
        )

        codes = _codes(
            validate_native(read_result, schema, wall_clock_budget_s=30.0),
        )

        self.assertIn(CODE_PATTERN_MISMATCH, codes)
        self.assertNotIn(CODE_TIMED_OUT, codes)
