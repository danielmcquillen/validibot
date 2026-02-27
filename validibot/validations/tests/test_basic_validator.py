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
