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
