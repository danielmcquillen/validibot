"""
Tests for the SimpleValidator base class.

Uses a concrete stub subclass (_StubSimpleValidator) that implements
the four abstract methods with controllable behavior to verify the
template method lifecycle.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

from django.test import TestCase

from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.base import ValidationIssue
from validibot.validations.validators.base.simple import SimpleValidator


class _StubSimpleValidator(SimpleValidator):
    """
    Concrete stub for testing the template method lifecycle.

    Each hook can be configured via constructor arguments to simulate
    different validation scenarios (file type rejection, parse failure,
    domain issues, signal extraction).
    """

    def __init__(
        self,
        *,
        file_type_issue: ValidationIssue | None = None,
        parsed_value: Any = None,
        parse_exception: Exception | None = None,
        domain_issues: list[ValidationIssue] | None = None,
        signals: dict[str, Any] | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._file_type_issue = file_type_issue
        self._parsed_value = parsed_value
        self._parse_exception = parse_exception
        self._domain_issues = domain_issues or []
        self._signals = signals or {}

    def validate_file_type(self, submission):
        return self._file_type_issue

    def parse_content(self, submission):
        if self._parse_exception:
            raise self._parse_exception
        return self._parsed_value

    def run_domain_checks(self, parsed):
        return list(self._domain_issues)

    def extract_signals(self, parsed):
        return dict(self._signals)


class SimpleValidatorLifecycleTests(TestCase):
    """Tests the template method validate() lifecycle on SimpleValidator."""

    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.user = UserFactory()
        cls.project = ProjectFactory(org=cls.org)
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            org=cls.org,
            is_system=False,
        )
        cls.ruleset = RulesetFactory(
            org=cls.org,
            ruleset_type=RulesetType.BASIC,
        )
        cls.submission = SubmissionFactory(
            org=cls.org,
            project=cls.project,
            user=cls.user,
            file_type=SubmissionFileType.JSON,
        )

    def test_happy_path_passes_with_no_issues(self):
        """Valid file type, successful parse, no domain issues -> passed=True."""
        engine = _StubSimpleValidator(parsed_value={"ok": True})

        result = engine.validate(self.validator, self.submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(result.issues, [])
        self.assertEqual(result.assertion_stats.total, 0)
        self.assertEqual(result.assertion_stats.failures, 0)

    def test_file_type_rejection_returns_early(self):
        """File type rejection returns immediately with passed=False."""
        issue = ValidationIssue(
            path="",
            message="Wrong file type",
            severity=Severity.ERROR,
        )
        engine = _StubSimpleValidator(file_type_issue=issue)

        result = engine.validate(self.validator, self.submission, self.ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].message, "Wrong file type")

    def test_parse_exception_returns_error(self):
        """When parse_content() raises, validate() returns a parse error."""
        engine = _StubSimpleValidator(
            parse_exception=ValueError("Invalid XML at line 42"),
        )

        result = engine.validate(self.validator, self.submission, self.ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("Invalid XML at line 42", result.issues[0].message)
        self.assertEqual(result.stats["parse_exception"], "ValueError")

    def test_domain_error_issues_fail_validation(self):
        """ERROR-severity domain issues cause passed=False."""
        engine = _StubSimpleValidator(
            parsed_value={},
            domain_issues=[
                ValidationIssue(
                    path="polygon.3",
                    message="Polygon is not closed",
                    severity=Severity.ERROR,
                ),
            ],
        )

        result = engine.validate(self.validator, self.submission, self.ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].path, "polygon.3")

    def test_domain_warning_issues_still_pass(self):
        """WARNING-severity domain issues do not cause failure."""
        engine = _StubSimpleValidator(
            parsed_value={},
            domain_issues=[
                ValidationIssue(
                    path="material.copper",
                    message="Conductivity unusually high",
                    severity=Severity.WARNING,
                ),
            ],
        )

        result = engine.validate(self.validator, self.submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].severity, Severity.WARNING)

    def test_signals_appear_in_result(self):
        """Signals from extract_signals() are included in ValidationResult."""
        engine = _StubSimpleValidator(
            parsed_value={},
            signals={"interior_bc_temp": 21.11, "polygon_count": 8},
        )

        result = engine.validate(self.validator, self.submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(result.signals["interior_bc_temp"], 21.11)
        self.assertEqual(result.signals["polygon_count"], 8)

    def test_empty_signals_result_in_none(self):
        """When extract_signals() returns empty dict, result.signals is None."""
        engine = _StubSimpleValidator(parsed_value={})

        result = engine.validate(self.validator, self.submission, self.ruleset)

        self.assertIsNone(result.signals)

    def test_run_context_stored_on_validator(self):
        """The run_context argument is stored on the validator instance."""
        engine = _StubSimpleValidator(parsed_value={})
        mock_context = MagicMock()

        engine.validate(
            self.validator,
            self.submission,
            self.ruleset,
            run_context=mock_context,
        )

        self.assertIs(engine.run_context, mock_context)

    def test_domain_issues_and_signals_coexist(self):
        """Domain warnings and signals can both appear in the result."""
        engine = _StubSimpleValidator(
            parsed_value={},
            domain_issues=[
                ValidationIssue(
                    path="mesh",
                    message="Mesh level is high",
                    severity=Severity.WARNING,
                ),
            ],
            signals={"mesh_level": 4},
        )

        result = engine.validate(self.validator, self.submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.signals["mesh_level"], 4)


class SimpleValidatorAssertionTests(TestCase):
    """Tests assertion evaluation within the SimpleValidator lifecycle."""

    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.user = UserFactory()
        cls.project = ProjectFactory(org=cls.org)
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            org=cls.org,
            is_system=False,
            allow_custom_assertion_targets=True,
        )
        cls.ruleset = RulesetFactory(
            org=cls.org,
            ruleset_type=RulesetType.BASIC,
        )
        cls.submission = SubmissionFactory(
            org=cls.org,
            project=cls.project,
            user=cls.user,
            file_type=SubmissionFileType.JSON,
            content=json.dumps({"price": 50}),
        )

    def test_assertion_evaluated_against_signals(self):
        """Assertions are evaluated using signals as the payload."""
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.BASIC,
            operator=AssertionOperator.LE,
            target_field="temperature",
            rhs={"value": 25.0},
            severity=Severity.ERROR,
            message_template="Temperature {{ actual }} exceeds {{ value }}",
        )
        engine = _StubSimpleValidator(
            parsed_value={},
            signals={"temperature": 30.0},
        )

        result = engine.validate(self.validator, self.submission, self.ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(result.assertion_stats.total, 1)
        self.assertEqual(result.assertion_stats.failures, 1)
        self.assertIn("30", result.issues[0].message)

    def test_passing_assertion(self):
        """An assertion that passes produces no ERROR issues."""
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.BASIC,
            operator=AssertionOperator.LE,
            target_field="temperature",
            rhs={"value": 25.0},
            severity=Severity.ERROR,
            message_template="Temperature too high",
        )
        engine = _StubSimpleValidator(
            parsed_value={},
            signals={"temperature": 20.0},
        )

        result = engine.validate(self.validator, self.submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(result.assertion_stats.total, 1)
        self.assertEqual(result.assertion_stats.failures, 0)
