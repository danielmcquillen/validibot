from __future__ import annotations

import json
from pathlib import Path

from django.test import TestCase

from simplevalidations.validations.constants import AssertionOperator
from simplevalidations.validations.constants import AssertionType
from simplevalidations.validations.constants import CatalogEntryType
from simplevalidations.validations.constants import CatalogRunStage
from simplevalidations.validations.constants import RulesetType
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.engines.basic import BasicValidatorEngine
from simplevalidations.validations.tests.factories import RulesetAssertionFactory
from simplevalidations.validations.tests.factories import RulesetFactory
from simplevalidations.validations.tests.factories import ValidatorCatalogEntryFactory
from simplevalidations.validations.tests.factories import ValidatorFactory


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
        # Catalog entries needed for CEL context resolution.
        cls.price_entry = ValidatorCatalogEntryFactory(
            validator=cls.validator,
            slug="price",
            run_stage=CatalogRunStage.INPUT,
            entry_type=CatalogEntryType.SIGNAL,
        )
        cls.rating_entry = ValidatorCatalogEntryFactory(
            validator=cls.validator,
            slug="rating",
            run_stage=CatalogRunStage.INPUT,
            entry_type=CatalogEntryType.SIGNAL,
        )
        cls.tags_entry = ValidatorCatalogEntryFactory(
            validator=cls.validator,
            slug="tags",
            run_stage=CatalogRunStage.INPUT,
            entry_type=CatalogEntryType.SIGNAL,
        )
        cls.ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        cls.expression = 'price > 0 && rating >= 90 && "mini" in tags'
        cls.error_message = (
            ' Your prices has to be greater than 0 AND rating greater '
            'than 90 AND "mini" '
            "has to be in the tags!!!"
        )

    def _assertion(self):
        return RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": self.expression},
            message_template=self.error_message,
        )

    def _engine_validate(self, payload: dict):
        self._assertion()
        engine = BasicValidatorEngine()
        # Basic engine expects JSON string content; we bypass submissions and 
        # feed payload directly.
        issues = engine.evaluate_cel_assertions(
            ruleset=self.ruleset,
            validator=self.validator,
            payload=payload,
            target_stage="input",
        )
        return issues

    def test_happy_path_passes(self):
        issues = self._engine_validate(self.payload)
        self.assertEqual(len(issues), 0)

    def test_price_must_be_positive(self):
        payload = {**self.payload, "price": 0}
        issues = self._engine_validate(payload)
        self.assertEqual(len(issues), 1)
        self.assertIn(self.error_message, issues[0].message)

    def test_missing_catalog_entry_reports_identifier(self):
        validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
            allow_custom_assertion_targets=False,
        )
        validator.allow_custom_assertion_targets = False
        validator.save(update_fields=["allow_custom_assertion_targets"])
        validator.refresh_from_db()
        self.assertFalse(validator.allow_custom_assertion_targets)
        ruleset = RulesetFactory(ruleset_type=RulesetType.BASIC)
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "price > 0"},
        )
        engine = BasicValidatorEngine()
        issues = engine.evaluate_cel_assertions(
            ruleset=ruleset,
            validator=validator,
            payload=self.payload,
            target_stage="input",
        )
        self.assertEqual(len(issues), 1)
        self.assertIn("identifier 'price'", issues[0].message)

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
