"""Tests for Table Schema ``missingValues`` handling in the Tabular Validator.

### What this suite covers and why

Frictionless Table Schema lets a descriptor declare ``missingValues`` — the raw
cell strings that mean "no value" (sentinels like ``NA``/``NULL``). Validibot V1
originally *preserved but ignored* them (only an empty cell was missing). This
suite pins the behaviour now that V1 **interprets** them, across the layer that
owns "what a cell is" (``coercion.coerce_cell``), the schema model + parser
(``schema.py``), the compatibility report, and native validation
(``native.validate_native``).

The contract being protected:

- A declared missing token coerces to **null**, exactly like an empty cell — so
  it satisfies neither a typed column (no spurious type error) nor a ``required``
  column (it *is* a required-value violation), and it is "no key" for
  ``primaryKey``/``unique``.
- The empty string is **always** missing. Declaring ``missingValues`` *extends*
  the set rather than replacing it, so an author cannot accidentally make blank
  cells count as present (the spec footgun we deliberately avoid).
- Default behaviour (no ``missingValues``) is unchanged: only ``""`` is missing,
  and a sentinel like ``NA`` is ordinary data (a type error in a typed column).
- The import preview no longer warns that ``missingValues`` is ignored, while it
  still warns about the genuinely-unenforced features (foreign keys, formats…).

These are pure functions (no DB), so the suite uses ``SimpleTestCase`` and
builds inputs through the real ``read_csv`` reader and the shipped basic-test
assets (``tests/assets/csv/basic_test/``).
"""

from __future__ import annotations

from django.test import SimpleTestCase

from tests.helpers.assets import load_json_test_asset
from tests.helpers.assets import load_test_asset
from validibot.validations.validators.tabular.coercion import coerce_cell
from validibot.validations.validators.tabular.native import CODE_ENUM_VIOLATION
from validibot.validations.validators.tabular.native import CODE_OUT_OF_RANGE
from validibot.validations.validators.tabular.native import CODE_PRIMARY_KEY_NULL
from validibot.validations.validators.tabular.native import CODE_REQUIRED_VALUE_MISSING
from validibot.validations.validators.tabular.native import CODE_TYPE_ERROR
from validibot.validations.validators.tabular.native import CODE_UNIQUE_VIOLATION
from validibot.validations.validators.tabular.native import validate_native
from validibot.validations.validators.tabular.readers.csv import read_csv
from validibot.validations.validators.tabular.schema import parse_table_schema
from validibot.validations.validators.tabular.schema import (
    table_schema_compatibility_notices,
)

# The basic-test fixtures the author added — an e-commerce "orders" Table Schema
# that exercises the full Frictionless vocabulary, plus a matching CSV.
_ASSET_DIR = "assets/csv/basic_test"


def _basic_schema() -> dict:
    """Load the basic-test Table Schema descriptor (asset)."""
    return load_json_test_asset(f"{_ASSET_DIR}/basic_test_schema.json")


def _basic_csv() -> bytes:
    """Load the basic-test CSV bytes (asset)."""
    return load_test_asset(f"{_ASSET_DIR}/basic_test.csv")


def _codes(findings) -> set[str]:
    """Collect the set of finding codes for concise assertions."""
    return {finding.code for finding in findings}


def _by_code(findings) -> dict[str, object]:
    """Index findings by code (codes are unique per validate_native call)."""
    return {finding.code: finding for finding in findings}


# ─────────────────────────────────────────────────────────────────────
# coerce_cell — the single authority on "is this cell null?"
# ─────────────────────────────────────────────────────────────────────
class MissingValueCoercionTests(SimpleTestCase):
    """``coerce_cell`` treats declared missing tokens as null, before typing."""

    def test_declared_token_is_null_regardless_of_type(self):
        """A declared sentinel is null even in a typed column.

        This is the crux: ``NA`` in a number column must read as *missing*, not
        as an uncoercible number — otherwise sentinel-using files would drown in
        spurious type errors instead of honest nullability findings.
        """
        for field_type in ("string", "integer", "number", "boolean", "date"):
            coerced = coerce_cell("NA", field_type, missing_values=("", "NA"))
            self.assertTrue(coerced.is_null, f"{field_type}: NA should be null")
            self.assertTrue(coerced.ok, f"{field_type}: NA is null, not a type error")

    def test_empty_is_always_null_even_with_custom_tokens(self):
        """The empty string stays null even when custom tokens are declared.

        Validibot always treats a blank cell as missing; declaring tokens
        extends the set, it never replaces ``""``.
        """
        self.assertTrue(coerce_cell("", "string", missing_values=("", "NA")).is_null)

    def test_default_does_not_treat_sentinels_as_missing(self):
        """Without a declared set, only ``""`` is missing — ``NA`` is data.

        Guards the backward-compatible default: a sentinel is ordinary text
        (here, a type error in an integer column), not silently null.
        """
        self.assertTrue(coerce_cell("", "string").is_null)
        not_missing = coerce_cell("NA", "integer")
        self.assertFalse(not_missing.is_null)
        self.assertFalse(not_missing.ok)  # "NA" is not a valid integer


# ─────────────────────────────────────────────────────────────────────
# Schema parsing — missingValues lands on the model
# ─────────────────────────────────────────────────────────────────────
class MissingValueSchemaParseTests(SimpleTestCase):
    """``parse_table_schema`` reads ``missingValues`` into the schema model."""

    def test_default_is_empty_string_only(self):
        """A descriptor without ``missingValues`` defaults to ``("",)``."""
        schema = parse_table_schema({"fields": [{"name": "a", "type": "string"}]})
        self.assertEqual(schema.missing_values, ("",))

    def test_declared_tokens_extend_and_always_include_empty(self):
        """Declared tokens are kept, with ``""`` always present and deduped.

        Even when the author omits ``""`` from their list, it is included first
        so blank cells remain missing.
        """
        schema = parse_table_schema(
            {
                "fields": [{"name": "a", "type": "string"}],
                "missingValues": ["NA", "NULL", "NA"],
            },
        )
        self.assertEqual(schema.missing_values, ("", "NA", "NULL"))

    def test_basic_asset_schema_missing_values(self):
        """The shipped asset's declared tokens parse onto the model."""
        schema = parse_table_schema(_basic_schema())
        self.assertEqual(schema.missing_values, ("", "NA", "NULL", "—"))


# ─────────────────────────────────────────────────────────────────────
# Compatibility report — missingValues is no longer "ignored"
# ─────────────────────────────────────────────────────────────────────
class MissingValueCompatibilityNoticeTests(SimpleTestCase):
    """The import preview stops claiming ``missingValues`` is unenforced."""

    def test_missing_values_notice_is_gone_but_others_remain(self):
        """No ``missing_values`` notice now that V1 enforces it.

        The other genuinely-unenforced features of the asset descriptor (foreign
        keys, exotic types, locale options, formats, unknown constraints) must
        still be reported, so this asserts the *absence* of one notice without
        silencing the rest.
        """
        codes = {n.code for n in table_schema_compatibility_notices(_basic_schema())}
        self.assertNotIn("missing_values", codes)
        self.assertEqual(
            codes,
            {
                "foreign_keys",
                "unsupported_types",
                "locale_options",
                "field_formats",
                "unsupported_constraints",
            },
        )


# ─────────────────────────────────────────────────────────────────────
# headline_html — schema tokens are <code>-styled, and HTML-safe
#
# The compatibility report wraps each field name / keyword copied from the
# descriptor in <code> so an author can see *which* schema entries a notice
# refers to. Because those names are author-controlled, this is also the one
# place a hostile descriptor reaches an HTML page — so the wrapping must escape.
# ─────────────────────────────────────────────────────────────────────
class CompatibilityHeadlineHtmlTests(SimpleTestCase):
    """``SchemaCompatibilityNotice.headline_html`` code-styles schema tokens."""

    def _notice(self, descriptor, code):
        """Return the single notice with ``code`` produced for ``descriptor``."""
        by_code = {n.code: n for n in table_schema_compatibility_notices(descriptor)}
        return by_code[code]

    def test_field_name_is_code_wrapped_with_type_note_outside(self):
        """An unsupported-type field renders ``<code>name</code> (type)``.

        Only the field name is code-styled; the parenthetical type is a plain
        qualifier, so the author's eye lands on the schema entry — the thing
        they can act on — rather than on the type annotation.
        """
        notice = self._notice(
            {"fields": [{"name": "order_year", "type": "year"}]},
            "unsupported_types",
        )
        self.assertIn("<code>order_year</code> (year)", str(notice.headline_html()))

    def test_multiple_items_are_each_wrapped_and_comma_joined(self):
        """Every named field gets its own ``<code>`` span, comma-separated.

        Guards against a single ``<code>`` swallowing the whole comma list,
        which would defeat the point of marking each individual schema entry.
        """
        notice = self._notice(
            {
                "fields": [
                    {"name": "first", "format": "email"},
                    {"name": "second", "format": "uri"},
                ],
            },
            "field_formats",
        )
        html = str(notice.headline_html())
        self.assertIn("<code>first</code>", html)
        self.assertIn("<code>second</code>", html)

    def test_hostile_field_name_is_escaped_inside_code(self):
        """A field named like an HTML tag is escaped, never rendered live.

        ``headline_html`` returns a SafeString the template prints unescaped, so
        the escaping has to happen here. A ``<script>`` field name must arrive in
        the page as inert text, not an executable tag.
        """
        notice = self._notice(
            {"fields": [{"name": "<script>x</script>", "type": "year"}]},
            "unsupported_types",
        )
        html = str(notice.headline_html())
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_plain_message_carries_no_markup(self):
        """``message`` stays a flat string for toasts/logs — no ``<code>``.

        The HTML lives only in ``headline_html``; the post-save toast path reads
        ``message`` and must not leak tags into that plain-text channel.
        """
        notice = self._notice(
            {"fields": [{"name": "order_year", "type": "year"}]},
            "unsupported_types",
        )
        self.assertNotIn("<code>", notice.message)
        self.assertEqual(
            notice.message,
            "Unsupported field types become Text: order_year (year).",
        )

    def test_itemless_notice_falls_back_to_message(self):
        """A notice that names no fields (foreign keys) prints its plain message.

        Not every notice has a field list; the ``<code>`` path must degrade to
        the flat sentence rather than emit an empty ``": ."`` tail.
        """
        notice = self._notice(
            {
                "foreignKeys": [{"fields": "id", "reference": {"resource": "x"}}],
                "fields": [{"name": "id", "type": "string"}],
            },
            "foreign_keys",
        )
        self.assertEqual(str(notice.headline_html()), notice.message)
        self.assertNotIn("<code>", str(notice.headline_html()))


# ─────────────────────────────────────────────────────────────────────
# Native validation — sentinels behave as null end-to-end
# ─────────────────────────────────────────────────────────────────────
class MissingValueNativeValidationTests(SimpleTestCase):
    """``validate_native`` honours ``missingValues`` for required/type/keys."""

    def test_required_column_flags_sentinel_and_empty_as_missing(self):
        """A required column treats both ``NA`` and a blank cell as missing.

        Confirms the sentinel reaches the nullability check, not just coercion:
        two rows (one ``NA``, one empty) produce one required-value finding
        covering both.
        """
        schema = parse_table_schema(
            {
                "fields": [
                    {
                        "name": "code",
                        "type": "string",
                        "constraints": {"required": True},
                    }
                ],
                "missingValues": ["NA"],
            },
        )
        # A second column keeps the empty `code` cell a real row (a single-column
        # blank line would be dropped as a trailing newline by the reader).
        findings = validate_native(read_csv(b"code,note\nABC,1\nNA,2\n,3\n"), schema)
        self.assertIn(CODE_REQUIRED_VALUE_MISSING, _codes(findings))
        self.assertEqual(_by_code(findings)[CODE_REQUIRED_VALUE_MISSING].count, 2)

    def test_sentinel_in_typed_column_is_null_not_type_error(self):
        """``NULL`` in an integer column is missing, not an invalid integer.

        Without missingValues this would be a ``type_error``; with it declared
        the cell is simply absent (and the column is optional here, so no
        finding at all).
        """
        schema = parse_table_schema(
            {
                "fields": [{"name": "n", "type": "integer"}],
                "missingValues": ["NULL"],
            },
        )
        findings = validate_native(read_csv(b"n\n5\nNULL\n"), schema)
        self.assertNotIn(CODE_TYPE_ERROR, _codes(findings))

    def test_sentinel_in_primary_key_is_a_null_key(self):
        """A sentinel in a primary-key column is a null key (forbidden).

        Primary keys must be present in every row, so a declared-missing token
        there is a ``primary_key_null`` violation, the same as a blank cell.
        """
        schema = parse_table_schema(
            {
                "fields": [{"name": "id", "type": "string"}],
                "primaryKey": "id",
                "missingValues": ["NA"],
            },
        )
        findings = validate_native(read_csv(b"id\nA\nNA\n"), schema)
        self.assertIn(CODE_PRIMARY_KEY_NULL, _codes(findings))

    def test_default_schema_treats_sentinel_as_type_error(self):
        """Control: with no declared tokens, ``NA`` is a type error, not null.

        Pins the behaviour difference the feature introduces, so a regression
        that ignored ``missingValues`` would fail loudly here.
        """
        schema = parse_table_schema({"fields": [{"name": "n", "type": "integer"}]})
        findings = validate_native(read_csv(b"n\n5\nNA\n"), schema)
        self.assertIn(CODE_TYPE_ERROR, _codes(findings))


# ─────────────────────────────────────────────────────────────────────
# Integration — the shipped basic-test assets through the real reader
# ─────────────────────────────────────────────────────────────────────
class BasicTestAssetIntegrationTests(SimpleTestCase):
    """Validate the basic-test CSV against its Table Schema, reader included."""

    def test_basic_csv_flags_the_invalid_row(self):
        """The asset CSV's deliberately-bad third data row trips every rule.

        Exercises the full native pipeline (reader → parse → native) on the
        real fixtures: the bad row violates required/enum/range/type, and its
        duplicate (order_id, sku) breaks the composite primary key.
        """
        schema = parse_table_schema(_basic_schema())
        findings = validate_native(read_csv(_basic_csv()), schema)
        codes = _codes(findings)
        self.assertIn(CODE_REQUIRED_VALUE_MISSING, codes)  # blank product_name
        self.assertIn(CODE_ENUM_VIOLATION, codes)  # category/currency
        self.assertIn(CODE_OUT_OF_RANGE, codes)  # quantity/unit_price/discount
        self.assertIn(CODE_TYPE_ERROR, codes)  # ordered_at/ship_date/is_gift
        self.assertIn(CODE_UNIQUE_VIOLATION, codes)  # duplicate primary key

    def test_asset_schema_missing_tokens_apply_to_the_real_columns(self):
        """``NA``/``NULL`` in required asset columns read as missing, not errors.

        Builds one otherwise-valid row from the asset header but with the
        declared sentinels in two required columns (a string and an integer).
        Both become required-value violations and — critically — the integer
        sentinel raises no type error.
        """
        header = _basic_csv().splitlines()[0]
        # Columns, in order: order_id, sku, product_name, category, quantity,
        # unit_price, discount_pct, currency, customer_email, ordered_at,
        # ship_date, is_gift, gift_message, order_year, pickup_time, warehouse_id
        row = (
            b"o-9001,ABC-9999,NA,electronics,NULL,9.99,0,USD,a@b.com,"
            b"2026-05-01T14:30:00,2026-05-03,false,,2026,09:00,WH1"
        )
        schema = parse_table_schema(_basic_schema())
        findings = validate_native(read_csv(header + b"\n" + row + b"\n"), schema)
        self.assertIn(CODE_REQUIRED_VALUE_MISSING, _codes(findings))
        # The integer column's "NULL" is missing, never an invalid-integer error.
        self.assertNotIn(CODE_TYPE_ERROR, _codes(findings))

    def test_same_row_without_missing_values_differs(self):
        """Control: drop ``missingValues`` and the same row behaves differently.

        Proves the asset's declared tokens are what changed the outcome: now
        ``NA`` is a present (valid) string so product_name is *not* missing, and
        ``NULL`` is an invalid integer (a type error).
        """
        header = _basic_csv().splitlines()[0]
        row = (
            b"o-9001,ABC-9999,NA,electronics,NULL,9.99,0,USD,a@b.com,"
            b"2026-05-01T14:30:00,2026-05-03,false,,2026,09:00,WH1"
        )
        descriptor = _basic_schema()
        descriptor.pop("missingValues", None)
        schema = parse_table_schema(descriptor)
        findings = validate_native(read_csv(header + b"\n" + row + b"\n"), schema)
        # quantity "NULL" is now an invalid integer.
        self.assertIn(CODE_TYPE_ERROR, _codes(findings))
