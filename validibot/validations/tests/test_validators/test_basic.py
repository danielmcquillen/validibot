from __future__ import annotations

import json

from django.test import TestCase

from validibot.actions.protocols import RunContext
from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.basic import BasicValidator


class BasicValidatorComparisonTests(TestCase):
    """
    Validates BASIC validator numeric comparisons and type coercion.

    These tests ensure arithmetic assertions remain reliable when payload values
    arrive as strings.
    """

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

    def test_numeric_string_coerces_when_enabled(self):
        """Ensures numeric strings are coerced before comparison when allowed."""

        RulesetAssertionFactory(
            ruleset=self.ruleset,
            operator=AssertionOperator.LT,
            target_data_path="price",
            rhs={"value": 20},
            options={"coerce_types": True},
            message_template="Price {{ actual }} exceeds {{ value }}",
        )
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
        )
        submission.content = json.dumps({"price": "25"})
        submission.save(update_fields=["content"])
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        issue = result.issues[0]
        self.assertEqual(issue.path, "price")
        self.assertEqual(issue.message, "Price 25 exceeds 20")

    def test_failure_message_template_interpolates_workflow_constants(self):
        """BASIC finding messages can reference workflow constants as ``c.*``.

        The runtime already injects constants so BASIC targets like ``c.name``
        can resolve. This test protects the separate rendering path that turns
        ``message_template`` into the persisted finding message.
        """
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            operator=AssertionOperator.EQ,
            target_data_path="name",
            rhs={"value": "bubba"},
            message_template=(
                "Submitted {{ p.name }}; not the same as bubba's value {{ c.bubba }}"
            ),
        )
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
        )
        submission.content = json.dumps({"name": "not-bubba"})
        submission.save(update_fields=["content"])
        run_context = RunContext(workflow_constants={"bubba": "dance"})
        engine = BasicValidator()

        result = engine.validate(
            self.validator,
            submission,
            self.ruleset,
            run_context=run_context,
        )

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(
            result.issues[0].message,
            "Submitted not-bubba; not the same as bubba's value dance",
        )

    def test_success_message_template_interpolates_workflow_constants(self):
        """BASIC success findings render constants with the same template context."""
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            operator=AssertionOperator.EQ,
            target_data_path="name",
            rhs={"value": "bubba"},
            success_message="Matched bubba's value {{ c.bubba }}",
        )
        submission = SubmissionFactory(
            org=self.org,
            project=self.project,
            user=self.user,
        )
        submission.content = json.dumps({"name": "bubba"})
        submission.save(update_fields=["content"])
        run_context = RunContext(workflow_constants={"bubba": "dance"})
        engine = BasicValidator()

        result = engine.validate(
            self.validator,
            submission,
            self.ruleset,
            run_context=run_context,
        )

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].message, "Matched bubba's value dance")
