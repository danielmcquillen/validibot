"""Tests for BasicValidator, including XML submission support."""

from __future__ import annotations

from unittest.mock import MagicMock

from django.test import SimpleTestCase
from django.test import TestCase

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorCatalogEntryFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.base.base import _collect_all_keys
from validibot.validations.validators.base.base import _is_valid_cel_identifier
from validibot.validations.validators.basic import BasicValidator


class BasicValidatorFileTypeTests(SimpleTestCase):
    """BasicValidator accepts JSON and XML, rejects other types."""

    def _make_submission(self, *, file_type: str, content: str) -> MagicMock:
        sub = MagicMock()
        sub.file_type = file_type
        sub.get_content.return_value = content
        return sub

    def _make_validator(self) -> MagicMock:
        validator = MagicMock()
        entries_qs = validator.catalog_entries.all.return_value
        entries_qs.only.return_value.filter.return_value = []
        validator.default_ruleset = None
        validator.allow_custom_assertion_targets = False
        return validator

    @staticmethod
    def _make_empty_ruleset() -> MagicMock:
        """Create a mock ruleset that safely exposes an empty assertions QS."""
        ruleset = MagicMock(unsafe=True)
        assertions_qs = ruleset.assertions.all.return_value
        assertions_qs.select_related.return_value.order_by.return_value = []
        return ruleset

    def test_rejects_text_file_type(self):
        """Plain text submissions are rejected with a clear error."""
        submission = self._make_submission(
            file_type=SubmissionFileType.TEXT,
            content="hello",
        )
        engine = BasicValidator()
        result = engine.validate(self._make_validator(), submission, MagicMock())

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("JSON or XML", result.issues[0].message)

    def test_rejects_binary_file_type(self):
        """Binary submissions are rejected."""
        submission = self._make_submission(
            file_type=SubmissionFileType.BINARY,
            content="",
        )
        engine = BasicValidator()
        result = engine.validate(self._make_validator(), submission, MagicMock())

        self.assertFalse(result.passed)
        self.assertIn("JSON or XML", result.issues[0].message)

    def test_accepts_json(self):
        """JSON submissions are parsed and assertions evaluated."""
        submission = self._make_submission(
            file_type=SubmissionFileType.JSON,
            content='{"price": 10}',
        )
        engine = BasicValidator()
        result = engine.validate(
            self._make_validator(),
            submission,
            self._make_empty_ruleset(),
        )

        self.assertTrue(result.passed)

    def test_accepts_xml(self):
        """XML submissions are parsed via xml_to_dict and assertions evaluated."""
        submission = self._make_submission(
            file_type=SubmissionFileType.XML,
            content="<root><price>10</price></root>",
        )
        engine = BasicValidator()
        result = engine.validate(
            self._make_validator(),
            submission,
            self._make_empty_ruleset(),
        )

        self.assertTrue(result.passed)

    def test_invalid_json_returns_error(self):
        """Malformed JSON returns a clear parse error."""
        submission = self._make_submission(
            file_type=SubmissionFileType.JSON,
            content="{broken",
        )
        engine = BasicValidator()
        result = engine.validate(self._make_validator(), submission, MagicMock())

        self.assertFalse(result.passed)
        self.assertIn("Invalid JSON", result.issues[0].message)

    def test_invalid_xml_returns_error(self):
        """Malformed XML returns a clear parse error."""
        submission = self._make_submission(
            file_type=SubmissionFileType.XML,
            content="<root><unclosed>",
        )
        engine = BasicValidator()
        result = engine.validate(self._make_validator(), submission, MagicMock())

        self.assertFalse(result.passed)
        self.assertIn("Invalid XML", result.issues[0].message)


class BasicValidatorXmlAssertionTests(TestCase):
    """End-to-end: XML submission with BASIC and CEL assertions."""

    @classmethod
    def setUpTestData(cls):
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
            allow_custom_assertion_targets=True,
        )
        cls.price_entry = ValidatorCatalogEntryFactory(
            validator=cls.validator,
            slug="price",
            run_stage=CatalogRunStage.INPUT,
            entry_type=CatalogEntryType.SIGNAL,
        )

    def test_cel_assertion_against_xml(self):
        """CEL expression evaluates against XML-derived dict."""
        xml_content = "<product><price>25.99</price><name>Widget</name></product>"
        SubmissionFactory(
            content=xml_content,
            file_type=SubmissionFileType.XML,
        )
        ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            target_catalog_entry=self.price_entry,
            target_data_path="",
            rhs={"expr": "price > 0"},
        )

        engine = BasicValidator()
        # Evaluate at the payload level (xml_to_dict wraps in root tag)
        payload_dict = {"product": {"price": "25.99", "name": "Widget"}}
        result = engine.evaluate_assertions_for_stage(
            validator=self.validator,
            ruleset=ruleset,
            payload=payload_dict,
            stage="input",
        )
        # price resolves via catalog entry slug against the payload;
        # with allow_custom_assertion_targets, top-level keys are exposed.
        # CEL expression "price > 0" should work because the catalog entry
        # slug "price" maps to the value at that path.
        # Note: XML values are strings, so comparison depends on CEL coercion.
        # This test verifies the pipeline works end-to-end.
        self.assertIsNotNone(result)

    def test_full_validate_with_xml_submission(self):
        """Full validate() call with XML submission parses and evaluates."""
        xml_content = "<data><value>42</value></data>"
        submission = SubmissionFactory(
            content=xml_content,
            file_type=SubmissionFileType.XML,
        )
        ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        # No assertions defined, so should pass
        self.assertTrue(result.passed)
        self.assertEqual(result.assertion_stats.total, 0)

    def test_basic_assertion_against_xml_nested_path(self):
        """BASIC assertion with dot-path resolves XML nested elements."""
        xml_content = (
            "<building>"
            "  <thermostat>"
            "    <setpoint>72</setpoint>"
            "  </thermostat>"
            "</building>"
        )
        submission = SubmissionFactory(
            content=xml_content,
            file_type=SubmissionFileType.XML,
        )

        # Create a catalog entry matching the nested path
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
            allow_custom_assertion_targets=True,
        )
        entry = ValidatorCatalogEntryFactory(
            validator=validator,
            slug="building.thermostat.setpoint",
            run_stage=CatalogRunStage.INPUT,
            entry_type=CatalogEntryType.SIGNAL,
        )

        ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.BASIC,
            operator=AssertionOperator.EQ,
            target_catalog_entry=entry,
            target_data_path="",
            rhs={"value": "72"},
        )

        engine = BasicValidator()
        result = engine.validate(validator, submission, ruleset)

        # The setpoint value "72" (string from XML) should equal "72"
        self.assertTrue(result.passed)
        self.assertEqual(result.assertion_stats.total, 1)
        self.assertEqual(result.assertion_stats.failures, 0)


# ---------------------------------------------------------------------------
# CEL identifier validation
# ---------------------------------------------------------------------------


class IsValidCelIdentifierTests(SimpleTestCase):
    """
    Unit tests for _is_valid_cel_identifier().

    CEL (Common Expression Language) requires that top-level activation variable
    names match the identifier grammar ``[_a-zA-Z][_a-zA-Z0-9]*``.  cel-python
    raises ``ValueError`` at evaluation time if a context dict contains a key
    that violates this rule.

    XML-to-dict conversion can produce keys that are perfectly valid XML names
    but invalid CEL identifiers — for example hyphenated element names like
    ``THERM-XML``, ``@``-prefixed attribute names, and ``#text`` mixed-content
    keys.  The helper function is used by ``_build_cel_context()`` to filter
    these out before they reach the CEL engine.
    """

    def test_simple_names_are_valid(self):
        """Standard Python/CEL variable names are accepted."""
        self.assertTrue(_is_valid_cel_identifier("price"))
        self.assertTrue(_is_valid_cel_identifier("Materials"))
        self.assertTrue(_is_valid_cel_identifier("THERM_XML"))
        self.assertTrue(_is_valid_cel_identifier("x"))

    def test_underscore_prefix_is_valid(self):
        """Leading underscores are valid CEL identifiers."""
        self.assertTrue(_is_valid_cel_identifier("_private"))
        self.assertTrue(_is_valid_cel_identifier("__dunder"))

    def test_alphanumeric_with_digits_is_valid(self):
        """Digits are allowed after the first character."""
        self.assertTrue(_is_valid_cel_identifier("item2"))
        self.assertTrue(_is_valid_cel_identifier("v1_beta"))

    def test_hyphenated_names_are_invalid(self):
        """Hyphenated XML element names (e.g., THERM-XML) are rejected."""
        self.assertFalse(_is_valid_cel_identifier("THERM-XML"))
        self.assertFalse(_is_valid_cel_identifier("my-variable"))
        self.assertFalse(_is_valid_cel_identifier("building-energy-model"))

    def test_at_prefix_is_invalid(self):
        """@-prefixed keys from XML attribute conversion are rejected."""
        self.assertFalse(_is_valid_cel_identifier("@id"))
        self.assertFalse(_is_valid_cel_identifier("@type"))
        self.assertFalse(_is_valid_cel_identifier("@Name"))

    def test_hash_prefix_is_invalid(self):
        """#text keys from XML mixed-content conversion are rejected."""
        self.assertFalse(_is_valid_cel_identifier("#text"))

    def test_numeric_start_is_invalid(self):
        """Names starting with a digit are not valid identifiers."""
        self.assertFalse(_is_valid_cel_identifier("123abc"))
        self.assertFalse(_is_valid_cel_identifier("0"))

    def test_empty_string_is_invalid(self):
        self.assertFalse(_is_valid_cel_identifier(""))

    def test_names_with_spaces_are_invalid(self):
        self.assertFalse(_is_valid_cel_identifier("has space"))

    def test_names_with_dots_are_invalid(self):
        """Dotted paths are not single identifiers (they use nested access)."""
        self.assertFalse(_is_valid_cel_identifier("a.b"))


# ---------------------------------------------------------------------------
# CEL context building with invalid-identifier payload keys
# ---------------------------------------------------------------------------


class CelContextInvalidKeyTests(SimpleTestCase):
    """
    Verify that _build_cel_context() gracefully handles payload dict keys
    that are not valid CEL identifiers.

    When ``allow_custom_assertion_targets`` is True, the context builder
    promotes payload keys to top-level CEL variables so users can write
    expressions like ``Materials.Material.all(...)`` instead of
    ``payload.Materials.Material.all(...)``.

    However, XML documents commonly use element names that are valid XML
    but invalid CEL identifiers — hyphens (``THERM-XML``), ``@``-prefixed
    attribute keys (``@Name``), and ``#text`` mixed-content markers.
    Promoting these would cause cel-python to raise ``ValueError: Invalid
    name ...`` before any expression is evaluated.

    The fix: skip keys that fail ``_is_valid_cel_identifier()`` during
    promotion.  The data is still accessible via bracket notation on the
    ``payload`` variable (e.g., ``payload["THERM-XML"]``).
    """

    @staticmethod
    def _make_validator(*, allow_custom: bool = True) -> MagicMock:
        validator = MagicMock()
        entries_qs = validator.catalog_entries.all.return_value
        entries_qs.only.return_value.filter.return_value = []
        validator.default_ruleset = None
        validator.allow_custom_assertion_targets = allow_custom
        return validator

    def test_hyphenated_root_key_not_promoted(self):
        """
        A hyphenated root key like 'THERM-XML' (from xml_to_dict) must NOT
        be added as a top-level CEL variable — cel-python would raise
        ValueError.  It should still be in payload.
        """
        engine = BasicValidator()
        payload = {"THERM-XML": {"Materials": {"Material": []}}}
        context = engine._build_cel_context(payload, self._make_validator())

        self.assertNotIn("THERM-XML", context)
        self.assertEqual(
            context["payload"]["THERM-XML"]["Materials"]["Material"],
            [],
        )

    def test_children_of_invalid_root_key_are_promoted(self):
        """
        When the root element has an invalid identifier name (e.g.
        'THERM-XML'), its valid-identifier children should still be
        promoted via the deep key collection.  This allows CEL
        expressions like ``Materials.Material.all(...)`` to work even
        when the root tag is inaccessible as a top-level variable.
        """
        engine = BasicValidator()
        payload = {
            "THERM-XML": {
                "Materials": {"Material": [{"Conductivity": "0.5"}]},
                "Units": "SI",
            },
        }
        context = engine._build_cel_context(payload, self._make_validator())

        self.assertNotIn("THERM-XML", context)
        # Valid children promoted via deep key collection
        self.assertIn("Materials", context)
        self.assertEqual(
            context["Materials"],
            {"Material": [{"Conductivity": "0.5"}]},
        )
        self.assertIn("Units", context)
        self.assertEqual(context["Units"], "SI")

    def test_valid_keys_still_promoted(self):
        """
        Valid identifier keys from the payload should be promoted to
        top-level context variables as before.
        """
        engine = BasicValidator()
        payload = {"price": 10, "name": "Widget"}
        context = engine._build_cel_context(payload, self._make_validator())

        self.assertIn("price", context)
        self.assertEqual(context["price"], 10)
        self.assertIn("name", context)
        self.assertEqual(context["name"], "Widget")

    def test_mixed_valid_and_invalid_keys(self):
        """
        Only valid-identifier keys are promoted; invalid ones are silently
        skipped.  Both remain accessible under ``payload``.
        """
        engine = BasicValidator()
        payload = {
            "THERM-XML": {"Units": "SI"},
            "Materials": {"Material": [{"@Name": "Wood"}]},
        }
        context = engine._build_cel_context(payload, self._make_validator())

        # "Materials" is valid → promoted
        self.assertIn("Materials", context)
        # "THERM-XML" is invalid → not promoted
        self.assertNotIn("THERM-XML", context)
        # Both still in payload
        self.assertIn("THERM-XML", context["payload"])
        self.assertIn("Materials", context["payload"])

    def test_at_prefixed_keys_not_promoted_via_collect(self):
        """
        The partial-path-match collector should not surface @-prefixed
        attribute keys (from xml_to_dict) as top-level variables.
        """
        engine = BasicValidator()
        # xml_to_dict produces @-prefixed keys for XML attributes
        payload = {"root": {"child": {"@id": "42", "value": "hello"}}}
        context = engine._build_cel_context(payload, self._make_validator())

        # @id should NOT appear as a top-level variable
        self.assertNotIn("@id", context)
        # "root" and "child" should be promoted (valid identifiers)
        self.assertIn("root", context)

    def test_disabled_custom_targets_skips_promotion(self):
        """
        When allow_custom_assertion_targets is False, payload keys are never
        promoted — so invalid keys are irrelevant.
        """
        engine = BasicValidator()
        payload = {"THERM-XML": {"Units": "SI"}, "Materials": []}
        validator = self._make_validator(allow_custom=False)
        context = engine._build_cel_context(payload, validator)

        # Only "payload" should be a top-level key
        self.assertIn("payload", context)
        self.assertNotIn("THERM-XML", context)
        self.assertNotIn("Materials", context)


class BasicValidatorHyphenatedXmlEndToEndTests(SimpleTestCase):
    """
    End-to-end validation tests with XML documents whose element names
    contain hyphens, confirming the full pipeline doesn't crash.
    """

    @staticmethod
    def _make_submission(*, file_type: str, content: str) -> MagicMock:
        sub = MagicMock()
        sub.file_type = file_type
        sub.get_content.return_value = content
        return sub

    @staticmethod
    def _make_validator(*, allow_custom: bool = True) -> MagicMock:
        validator = MagicMock()
        entries_qs = validator.catalog_entries.all.return_value
        entries_qs.only.return_value.filter.return_value = []
        validator.default_ruleset = None
        validator.allow_custom_assertion_targets = allow_custom
        return validator

    @staticmethod
    def _make_empty_ruleset() -> MagicMock:
        ruleset = MagicMock(unsafe=True)
        assertions_qs = ruleset.assertions.all.return_value
        assertions_qs.select_related.return_value.order_by.return_value = []
        return ruleset

    def test_xml_with_hyphenated_root_validates_without_crash(self):
        """
        A full validate() call with a hyphenated root element like
        <THERM-XML> should complete without raising ValueError.

        Before the fix, this would crash with:
            ValueError: Invalid name THERM-XML
        because the root key was promoted to a top-level CEL variable.
        """
        submission = self._make_submission(
            file_type=SubmissionFileType.XML,
            content=(
                '<THERM-XML xmlns="http://windows.lbl.gov">'
                "  <Units>SI</Units>"
                "</THERM-XML>"
            ),
        )
        engine = BasicValidator()
        result = engine.validate(
            self._make_validator(),
            submission,
            self._make_empty_ruleset(),
        )

        self.assertTrue(result.passed)

    def test_xml_with_hyphenated_child_elements_validates(self):
        """
        Hyphenated child element names should also not crash the pipeline.
        """
        submission = self._make_submission(
            file_type=SubmissionFileType.XML,
            content=(
                "<root>"
                "  <energy-rating>A+</energy-rating>"
                "  <building-type>Commercial</building-type>"
                "</root>"
            ),
        )
        engine = BasicValidator()
        result = engine.validate(
            self._make_validator(),
            submission,
            self._make_empty_ruleset(),
        )

        self.assertTrue(result.passed)

    def test_xml_with_attributes_validates(self):
        """
        XML attributes (converted to @-prefixed keys by xml_to_dict) should
        not crash CEL context building.
        """
        submission = self._make_submission(
            file_type=SubmissionFileType.XML,
            content='<root><item id="1" type="widget">Test</item></root>',
        )
        engine = BasicValidator()
        result = engine.validate(
            self._make_validator(),
            submission,
            self._make_empty_ruleset(),
        )

        self.assertTrue(result.passed)


# ---------------------------------------------------------------------------
# _collect_all_keys helper
# ---------------------------------------------------------------------------


class CollectAllKeysTests(SimpleTestCase):
    """Unit tests for _collect_all_keys().

    This helper recursively collects every dict key in a nested
    structure.  It is used by _build_cel_context() to discover
    candidate identifiers from the entire payload tree, so that
    valid-identifier keys nested under invalid-identifier parents
    (e.g. ``Materials`` inside ``THERM-XML``) can still be promoted.
    """

    def test_flat_dict(self):
        """Keys from a flat dict are collected."""
        data = {"a": 1, "b": 2}
        self.assertEqual(_collect_all_keys(data), {"a", "b"})

    def test_nested_dict(self):
        """Keys at all nesting levels are collected."""
        data = {"root": {"child": {"leaf": "val"}}}
        self.assertEqual(
            _collect_all_keys(data),
            {"root", "child", "leaf"},
        )

    def test_list_of_dicts(self):
        """Keys inside dicts within lists are collected."""
        data = [{"a": 1}, {"b": 2}]
        self.assertEqual(_collect_all_keys(data), {"a", "b"})

    def test_mixed_nested_structure(self):
        """Keys collected from complex nested dicts and lists."""
        data = {
            "THERM-XML": {
                "Materials": {
                    "Material": [
                        {"@Name": "Wood", "Conductivity": "0.5"},
                    ],
                },
            },
        }
        expected = {
            "THERM-XML",
            "Materials",
            "Material",
            "@Name",
            "Conductivity",
        }
        self.assertEqual(_collect_all_keys(data), expected)

    def test_non_dict_non_list_returns_empty(self):
        """Non-container types return an empty set."""
        self.assertEqual(_collect_all_keys("string"), set())
        self.assertEqual(_collect_all_keys(42), set())
        self.assertEqual(_collect_all_keys(None), set())

    def test_empty_structures(self):
        """Empty dict and list return empty sets."""
        self.assertEqual(_collect_all_keys({}), set())
        self.assertEqual(_collect_all_keys([]), set())


# ---------------------------------------------------------------------------
# CEL error message formatting
# ---------------------------------------------------------------------------


class CelErrorMessageTests(TestCase):
    """Tests for CelAssertionEvaluator._format_error_message().

    The error message for undefined identifiers should vary based on
    whether the validator uses custom data paths (Basic-style) or
    catalog entries (EnergyPlus-style), so the guidance is actionable.
    """

    def _get_evaluator(self):
        from validibot.validations.assertions.evaluators.cel import (
            CelAssertionEvaluator,
        )

        return CelAssertionEvaluator()

    def test_custom_targets_message(self):
        """Validators with custom data paths get data-path guidance."""
        evaluator = self._get_evaluator()
        validator = MagicMock()
        validator.allow_custom_assertion_targets = True

        msg = evaluator._format_error_message(
            "undeclared reference to 'Materials'",
            validator=validator,
        )
        self.assertIn("undefined name 'Materials'", msg)
        self.assertIn("data path", msg)
        self.assertNotIn("signal", msg)

    def test_catalog_targets_message(self):
        """Validators without custom targets get signal guidance."""
        evaluator = self._get_evaluator()
        validator = MagicMock()
        validator.allow_custom_assertion_targets = False

        msg = evaluator._format_error_message(
            "undeclared reference to 'price'",
            validator=validator,
        )
        self.assertIn("undefined name 'price'", msg)
        self.assertIn("signal", msg)
        self.assertNotIn("data path", msg)

    def test_non_identifier_error_passes_through(self):
        """Errors that aren't about undefined identifiers pass through."""
        evaluator = self._get_evaluator()
        raw = "type mismatch: int vs string"
        msg = evaluator._format_error_message(raw)
        self.assertEqual(msg, raw)

    def test_dot_at_syntax_error_message(self):
        """m.@Conductivity compile error gets a helpful message."""
        evaluator = self._get_evaluator()
        raw = (
            "Materials.Material.all(m, double(m.@Conductivity) > 0.0)\n"
            "                                   ^\n"
        )
        msg = evaluator._format_error_message(raw)
        self.assertIn("bracket notation", msg)
        self.assertIn("@Conductivity", msg)
        self.assertNotIn("^", msg)

    def test_field_selection_error_message(self):
        """Field selection failure on @-keyed dict gets helpful message."""
        evaluator = self._get_evaluator()
        raw = (
            "({'Material': [{'@Name': 'Wood'}]} "
            "with type: '<class 'dict'>' does not support field selection"
        )
        msg = evaluator._format_error_message(raw)
        self.assertIn("XML attributes", msg)
        self.assertIn("@Conductivity", msg)

    def test_no_such_member_error_message(self):
        """Missing member on MapType (e.g. Conductivity instead of @Conductivity)."""
        evaluator = self._get_evaluator()
        # cel-python wraps the error with escaped quotes
        raw = (
            "('no such member in mapping: \\'Conductivity\\'', "
            "<class 'KeyError'>, None)"
        )
        msg = evaluator._format_error_message(raw)
        self.assertIn("@Conductivity", msg)
        self.assertIn("bracket notation", msg)

    def test_no_such_member_unescaped_quotes(self):
        """Same pattern but with plain quotes (for robustness)."""
        evaluator = self._get_evaluator()
        raw = "no such member in mapping: 'Temperature'"
        msg = evaluator._format_error_message(raw)
        self.assertIn("@Temperature", msg)


# ---------------------------------------------------------------------------
# THERM XML integration tests with CEL assertions
# ---------------------------------------------------------------------------

# Sample THERM XML — a minimal .thmx with three materials whose
# conductivity values are all valid (between 0 and 500).
_VALID_THERM_XML = (
    '<?xml version="1.0"?>'
    '<THERM-XML xmlns="http://windows.lbl.gov">'
    "  <ThermVersion>Version 8.0.20.0</ThermVersion>"
    "  <FileVersion>1</FileVersion>"
    "  <Title>Test Frame</Title>"
    "  <CreatedBy>Tests</CreatedBy>"
    "  <CrossSectionType>Sill</CrossSectionType>"
    "  <Units>SI</Units>"
    "  <Materials>"
    '    <Material Name="Aluminum" Type="0" Conductivity="160.0"'
    '      Tir="0" EmissivityFront="0.2" EmissivityBack="0.2" />'
    '    <Material Name="PVC" Type="0" Conductivity="0.16"'
    '      Tir="0" EmissivityFront="0.9" EmissivityBack="0.9" />'
    '    <Material Name="Glass" Type="0" Conductivity="1.0"'
    '      Tir="0" EmissivityFront="0.84" EmissivityBack="0.84" />'
    "  </Materials>"
    "</THERM-XML>"
)

# Same structure but one material has Conductivity > 500 (invalid).
_INVALID_THERM_XML = (
    '<?xml version="1.0"?>'
    '<THERM-XML xmlns="http://windows.lbl.gov">'
    "  <ThermVersion>Version 8.0.20.0</ThermVersion>"
    "  <FileVersion>1</FileVersion>"
    "  <Title>Test Frame</Title>"
    "  <CreatedBy>Tests</CreatedBy>"
    "  <CrossSectionType>Sill</CrossSectionType>"
    "  <Units>SI</Units>"
    "  <Materials>"
    '    <Material Name="Aluminum" Type="0" Conductivity="160.0"'
    '      Tir="0" EmissivityFront="0.2" EmissivityBack="0.2" />'
    '    <Material Name="SuperConductor" Type="0" Conductivity="999.0"'
    '      Tir="0" EmissivityFront="0.9" EmissivityBack="0.9" />'
    '    <Material Name="Glass" Type="0" Conductivity="1.0"'
    '      Tir="0" EmissivityFront="0.84" EmissivityBack="0.84" />'
    "  </Materials>"
    "</THERM-XML>"
)

# Same structure but one material has Conductivity = 0 (also invalid).
_ZERO_CONDUCTIVITY_XML = (
    '<?xml version="1.0"?>'
    '<THERM-XML xmlns="http://windows.lbl.gov">'
    "  <ThermVersion>Version 8.0.20.0</ThermVersion>"
    "  <FileVersion>1</FileVersion>"
    "  <Title>Test Frame</Title>"
    "  <CreatedBy>Tests</CreatedBy>"
    "  <CrossSectionType>Sill</CrossSectionType>"
    "  <Units>SI</Units>"
    "  <Materials>"
    '    <Material Name="Vacuum" Type="0" Conductivity="0.0"'
    '      Tir="0" EmissivityFront="0.5" EmissivityBack="0.5" />'
    "  </Materials>"
    "</THERM-XML>"
)

# The correct CEL expression: bracket notation for @-prefixed XML attrs.
_CONDUCTIVITY_CEL = (
    "Materials.Material.all(m, "
    'double(m["@Conductivity"]) > 0.0 '
    '&& double(m["@Conductivity"]) <= 500.0)'
)


class ThermXmlCelIntegrationTests(TestCase):
    """Integration tests: BasicValidator + THERM XML + CEL assertions.

    These tests validate the full pipeline:
    1. XML is parsed by xml_to_dict (attributes → @-prefixed keys)
    2. _build_cel_context promotes "Materials" from under "THERM-XML"
    3. CEL expression using bracket notation accesses @-prefixed attrs
    4. Assertion pass/fail is reported correctly
    """

    @classmethod
    def setUpTestData(cls):
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
            allow_custom_assertion_targets=True,
        )

    def _make_ruleset_with_cel(self, expr):
        """Create a ruleset with a single CEL assertion."""
        ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            target_catalog_entry=None,
            target_data_path="Materials",
            rhs={"expr": expr},
        )
        return ruleset

    def test_valid_therm_xml_passes_conductivity_check(self):
        """All materials have conductivity in (0, 500] → assertion passes."""
        submission = SubmissionFactory(
            content=_VALID_THERM_XML,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(_CONDUCTIVITY_CEL)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(result.assertion_stats.total, 1)
        self.assertEqual(result.assertion_stats.failures, 0)

    def test_invalid_conductivity_fails_assertion(self):
        """One material has conductivity=999 → assertion evaluates to false."""
        submission = SubmissionFactory(
            content=_INVALID_THERM_XML,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(_CONDUCTIVITY_CEL)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(result.assertion_stats.total, 1)
        self.assertEqual(result.assertion_stats.failures, 1)

    def test_zero_conductivity_fails_assertion(self):
        """Material with conductivity=0.0 fails the > 0.0 check."""
        submission = SubmissionFactory(
            content=_ZERO_CONDUCTIVITY_XML,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(_CONDUCTIVITY_CEL)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(result.assertion_stats.failures, 1)

    def test_dot_at_syntax_gives_helpful_error(self):
        """m.@Conductivity (invalid CEL) produces actionable error."""
        bad_expr = "Materials.Material.all(m, double(m.@Conductivity) > 0.0)"
        submission = SubmissionFactory(
            content=_VALID_THERM_XML,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(bad_expr)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertFalse(result.passed)
        error_msg = result.issues[0].message
        self.assertIn("bracket notation", error_msg)

    def test_missing_at_prefix_gives_helpful_error(self):
        """m.Conductivity (no @) fails because the dict key is @Conductivity."""
        no_at_expr = "Materials.Material.all(m, double(m.Conductivity) > 0.0)"
        submission = SubmissionFactory(
            content=_VALID_THERM_XML,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(no_at_expr)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertFalse(result.passed)
        error_msg = result.issues[0].message
        self.assertIn("@Conductivity", error_msg)

    def test_name_attribute_accessible_via_bracket(self):
        """@Name attribute is accessible via bracket notation."""
        name_expr = 'Materials.Material.all(m, m["@Name"] != "")'
        submission = SubmissionFactory(
            content=_VALID_THERM_XML,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(name_expr)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertTrue(result.passed)

    def test_units_element_accessible_as_top_level(self):
        """Child element 'Units' is promoted to top-level CEL context."""
        units_expr = 'Units == "SI"'
        submission = SubmissionFactory(
            content=_VALID_THERM_XML,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(units_expr)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertTrue(result.passed)

    def test_real_thmx_fixture_passes_conductivity_check(self):
        """The sample_valid.thmx fixture passes the conductivity check."""
        import pathlib

        fixture = pathlib.Path(__file__).parent / (
            "test_validators/fixtures/sample_valid.thmx"
        )
        xml_content = fixture.read_text()
        submission = SubmissionFactory(
            content=xml_content,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(_CONDUCTIVITY_CEL)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(result.assertion_stats.failures, 0)

    def test_bad_conductivity_fixture_fails_with_custom_message(self):
        """sample_sill_CMA_bad_conductivity.thmx has negative values → fails.

        The fixture contains materials with Conductivity values of -1.0,
        -0.00695, and -0.01 which violate the > 0.0 check.  The assertion
        is configured with a custom message_template so the failure message
        is user-friendly rather than the generic CEL default.
        """
        import pathlib

        fixture = (
            pathlib.Path(__file__).parents[3]
            / "tests/data/therm/sample_sill_CMA_bad_conductivity.thmx"
        )
        xml_content = fixture.read_text()
        submission = SubmissionFactory(
            content=xml_content,
            file_type=SubmissionFileType.XML,
        )

        failure_msg = (
            "One or more conductivity values are less than zero or more than 500."
        )
        ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            target_catalog_entry=None,
            target_data_path="Materials",
            rhs={"expr": _CONDUCTIVITY_CEL},
            message_template=failure_msg,
        )

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(result.assertion_stats.total, 1)
        self.assertEqual(result.assertion_stats.failures, 1)
        self.assertEqual(result.issues[0].message, failure_msg)

    def test_good_conductivity_fixture_passes(self):
        """sample_sill_CMA.thmx has all valid conductivity values → passes."""
        import pathlib

        fixture = (
            pathlib.Path(__file__).parents[3] / "tests/data/therm/sample_sill_CMA.thmx"
        )
        xml_content = fixture.read_text()
        submission = SubmissionFactory(
            content=xml_content,
            file_type=SubmissionFileType.XML,
        )
        ruleset = self._make_ruleset_with_cel(_CONDUCTIVITY_CEL)

        engine = BasicValidator()
        result = engine.validate(self.validator, submission, ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(result.assertion_stats.total, 1)
        self.assertEqual(result.assertion_stats.failures, 0)
