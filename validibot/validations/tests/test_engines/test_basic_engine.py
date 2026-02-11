from __future__ import annotations

import json

from django.test import TestCase

from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.engines.basic import BasicValidatorEngine
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory


class BasicEngineComparisonTests(TestCase):
    """
    Validates BASIC engine numeric comparisons and type coercion.

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
            target_field="price",
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
        engine = BasicValidatorEngine()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        issue = result.issues[0]
        self.assertEqual(issue.path, "price")
        self.assertEqual(issue.message, "Price 25 exceeds 20")
