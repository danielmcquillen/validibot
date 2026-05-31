"""
Tests for the Tabular Validator's schema inference
(``validators/tabular/infer.py``).

### What this suite covers and why

Inferring a schema from a sample CSV is the fastest setup path — "drop a file,
get a Table Schema to tighten." The behaviours pinned here are the ones that
make the inferred schema trustworthy as a starting point:

- **Type guessing is locale-free and reuses the validator's coercion**, so an
  inferred type means what validation will enforce.
- **Candidate order is deliberate** — ``integer`` before ``boolean`` (so
  ``0``/``1`` reads as integer), mixed columns fall back to ``string``.
- **The descriptor round-trips** through ``parse_table_schema``.
- **Sampling is bounded** — inference reads only the sample, so a value past
  the sample window doesn't change the inferred type (and a large file doesn't
  get fully loaded).

Pure functions (no DB) → ``SimpleTestCase``.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from validibot.validations.validators.tabular.infer import infer_table_schema
from validibot.validations.validators.tabular.preflight import TabularDialect
from validibot.validations.validators.tabular.schema import parse_table_schema


def _types(result):
    """Map column name → inferred type for concise assertions."""
    return {field["name"]: field["type"] for field in result.descriptor["fields"]}


class InferTypeTests(SimpleTestCase):
    """Per-column type guessing from sampled values."""

    def test_infers_common_scalar_types(self):
        """Integer, number, boolean, date, and string columns are each typed
        from their values — the baseline that makes inference useful.
        """
        content = (
            b"i,f,b,d,s\n1,1.5,true,2020-01-02,hello\n2,2.5,false,2021-03-04,world\n"
        )
        self.assertEqual(
            _types(infer_table_schema(content)),
            {
                "i": "integer",
                "f": "number",
                "b": "boolean",
                "d": "date",
                "s": "string",
            },
        )

    def test_zero_one_column_is_integer_not_boolean(self):
        """``0``/``1`` reads as integer, because ``integer`` is tried before
        ``boolean`` — a numeric flag column shouldn't silently become boolean.
        """
        self.assertEqual(
            _types(infer_table_schema(b"flag\n0\n1\n0\n"))["flag"], "integer"
        )

    def test_mixed_column_falls_back_to_string(self):
        """A column whose values don't all fit one type stays ``string`` — the
        safe default that accepts anything, leaving the author to decide.
        """
        self.assertEqual(_types(infer_table_schema(b"n\n1\nabc\n"))["n"], "string")

    def test_all_empty_column_is_string(self):
        """A column with no non-empty values can't be typed, so it defaults to
        ``string`` rather than guessing.
        """
        types = _types(infer_table_schema(b"a,b\n1,\n2,\n"))
        self.assertEqual(types["a"], "integer")
        self.assertEqual(types["b"], "string")


class InferDialectAndNamesTests(SimpleTestCase):
    """Dialect resolution and column naming flow through inference."""

    def test_sniffed_delimiter_is_returned(self):
        """A tab-delimited sample is detected, and the resolved dialect is
        returned for storage so a re-read uses the same delimiter.
        """
        result = infer_table_schema(b"a\tb\n1\t2\n3\t4\n")
        self.assertEqual(result.dialect.delimiter, "\t")
        self.assertEqual(_types(result), {"a": "integer", "b": "integer"})

    def test_headerless_synthesises_names(self):
        """With ``has_header=false`` the inferred fields use synthesised
        ``column_N`` names, still typed from the values.
        """
        result = infer_table_schema(
            b"1,2.5\n3,4.5\n",
            dialect=TabularDialect(has_header=False),
        )
        self.assertEqual(
            _types(result),
            {"column_1": "integer", "column_2": "number"},
        )
        self.assertFalse(result.dialect.has_header)

    def test_descriptor_round_trips_through_parser(self):
        """The inferred descriptor is a valid Table Schema — it parses back
        into the internal model, so inference output can be stored and reused
        directly.
        """
        result = infer_table_schema(b"lat,name\n10,a\n20,b\n")
        schema = parse_table_schema(result.descriptor)
        self.assertEqual(schema.field_names(), ["lat", "name"])
        self.assertEqual(schema.fields[0].type, "integer")


class InferSamplingTests(SimpleTestCase):
    """Inference reads only the sample window."""

    def test_value_past_sample_window_does_not_change_type(self):
        """With ``sample_rows=1``, only the first data row is seen — so a
        non-integer value on row 2 doesn't pull the column to ``string``. This
        proves inference (and the reader's sample mode) bounds the read.
        """
        # Full read would infer string (mixed); sampling 1 row sees only "1".
        result = infer_table_schema(b"n\n1\nabc\n", sample_rows=1)
        self.assertEqual(_types(result)["n"], "integer")
