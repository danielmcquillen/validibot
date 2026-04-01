from __future__ import annotations

import json
from pathlib import Path

from django.test import TestCase

from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import SignalDefinitionFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.basic import BasicValidator


class TestExampleProductWithCEL(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.payload = json.loads(
            Path("tests/assets/json/example_product.json").read_text(),
        )
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        # Signal definitions needed for CEL context resolution.
        cls.price_entry = SignalDefinitionFactory(
            validator=cls.validator,
            contract_key="price",
            direction="input",
        )
        cls.rating_entry = SignalDefinitionFactory(
            validator=cls.validator,
            contract_key="rating",
            direction="input",
        )
        cls.tags_entry = SignalDefinitionFactory(
            validator=cls.validator,
            contract_key="tags",
            direction="input",
        )
        cls.expression = 'p.price > 0 && p.rating >= 90 && "mini" in p.tags'
        cls.error_message = (
            " Your prices has to be greater than 0 AND rating greater "
            'than 90 AND "mini" '
            "has to be in the tags!!!"
        )

    def setUp(self):
        """Create fresh ruleset for each test to avoid assertion accumulation."""
        self.ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)

    def _assertion(self):
        return RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            # Set target_signal_definition to an input signal so the assertion
            # runs in input stage (resolved_run_stage defaults to OUTPUT
            # when target_signal_definition is None).
            target_signal_definition=self.price_entry,
            # Required: must be empty when signal definition is set
            target_data_path="",
            rhs={"expr": self.expression},
            message_template=self.error_message,
        )

    def _engine_validate(self, payload: dict):
        self._assertion()
        engine = BasicValidator()
        # Basic validator expects JSON string content; we bypass submissions and
        # feed payload directly.
        result = engine.evaluate_assertions_for_stage(
            ruleset=self.ruleset,
            validator=self.validator,
            payload=payload,
            stage="input",
        )
        return result.issues

    def test_happy_path_passes(self):
        issues = self._engine_validate(self.payload)
        self.assertEqual(len(issues), 0)

    def test_price_must_be_positive(self):
        payload = {**self.payload, "price": 0}
        issues = self._engine_validate(payload)
        self.assertEqual(len(issues), 1)
        self.assertIn(self.error_message, issues[0].message)

    def test_missing_signal_reports_evaluation_error(self):
        """Accessing a signal that doesn't exist in the s namespace
        produces a CEL evaluation error. The s namespace is only
        populated by workflow-level signal mappings and promoted
        outputs, not by validator input signal definitions.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
        )
        ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "s.nonexistent_signal > 0"},
        )
        engine = BasicValidator()
        result = engine.evaluate_assertions_for_stage(
            ruleset=ruleset,
            validator=validator,
            payload=self.payload,
            stage="output",
        )
        self.assertEqual(len(result.issues), 1)
        # s.nonexistent_signal triggers a CEL field-not-found error
        # because no workflow signal with that name exists.
        self.assertIn("CEL evaluation failed", result.issues[0].message)

    def test_rating_must_be_high_enough(self):
        payload = {**self.payload, "rating": 80}
        issues = self._engine_validate(payload)
        self.assertEqual(len(issues), 1)
        self.assertIn(self.error_message, issues[0].message)

    def test_mini_must_be_in_tags(self):
        payload = {**self.payload, "tags": ["gadgets"]}
        issues = self._engine_validate(payload)
        self.assertEqual(len(issues), 1)
        self.assertIn(self.error_message, issues[0].message)
