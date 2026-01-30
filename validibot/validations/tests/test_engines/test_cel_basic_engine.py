from __future__ import annotations

import json

from django.test import TestCase
from django.test import override_settings

from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.engines.basic import BasicValidatorEngine
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorCatalogEntryFactory
from validibot.validations.tests.factories import ValidatorFactory


@override_settings(ENABLE_DERIVED_SIGNALS=True)
class CelBasicEngineTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
            org=cls.org,
        )
        # Catalog entries spanning input/output/derived
        cls.input_signal = ValidatorCatalogEntryFactory(
            validator=cls.validator,
            slug="price",
            run_stage=CatalogRunStage.INPUT,
            entry_type=CatalogEntryType.SIGNAL,
        )
        cls.output_signal = ValidatorCatalogEntryFactory(
            validator=cls.validator,
            slug="result.total",
            run_stage=CatalogRunStage.OUTPUT,
            entry_type=CatalogEntryType.SIGNAL,
        )
        cls.output_status = ValidatorCatalogEntryFactory(
            validator=cls.validator,
            slug="result.status",
            run_stage=CatalogRunStage.OUTPUT,
            entry_type=CatalogEntryType.SIGNAL,
        )
        cls.derived_signal = ValidatorCatalogEntryFactory(
            validator=cls.validator,
            slug="metrics.avg",
            run_stage=CatalogRunStage.INPUT,
            entry_type=CatalogEntryType.DERIVATION,
        )
        cls.required_entry = ValidatorCatalogEntryFactory(
            validator=cls.validator,
            slug="required_value",
            is_required=True,
            run_stage=CatalogRunStage.INPUT,
        )
        cls.list_signal = ValidatorCatalogEntryFactory(
            validator=cls.validator,
            slug="items",
            run_stage=CatalogRunStage.INPUT,
            entry_type=CatalogEntryType.SIGNAL,
        )
        cls.ruleset = RulesetFactory(
            org=cls.org,
            ruleset_type=RulesetType.BASIC,
        )
        cls.project = ProjectFactory(org=cls.org)

    def _submission(self, payload: dict) -> SubmissionFactory:
        submission = SubmissionFactory(org=self.org, project=self.project)
        submission.content = json.dumps(payload)
        submission.save(update_fields=["content"])
        return submission

    def test_true_expression_on_input_signal(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "price < 10"},
        )
        submission = self._submission({"price": 5})
        engine = BasicValidatorEngine()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 0)

    def test_list_size_helper(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "size(items) == 2"},
        )
        submission = self._submission({"items": [{"sku": "A"}, {"sku": "B"}]})
        engine = BasicValidatorEngine()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 0)

    def test_false_expression_on_input_signal(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "price < 10"},
            severity=Severity.ERROR,
        )
        submission = self._submission({"price": 25})
        engine = BasicValidatorEngine()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("Peak too high", result.issues[0].message)

    def test_when_guard_skips_expression(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "price < 10"},
            when_expression="price > 100",
        )
        submission = self._submission({"price": 20})
        engine = BasicValidatorEngine()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 0)

    def test_invalid_expression_reports_error(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "price < "},  # invalid CEL
        )
        submission = self._submission({"price": 5})
        engine = BasicValidatorEngine()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("CEL evaluation failed", result.issues[0].message)

    def test_dotted_slug_resolution(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "metrics.avg == 3"},
        )
        submission = self._submission({"metrics": {"avg": 3}})
        engine = BasicValidatorEngine()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 0)

    def test_missing_required_signal_sets_none(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "required_value == null"},
        )
        submission = self._submission({"price": 5})
        engine = BasicValidatorEngine()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 0)

    def test_matches_helper_on_input(self):
        slug = "serial"
        ValidatorCatalogEntryFactory(
            validator=self.validator,
            slug=slug,
            run_stage=CatalogRunStage.INPUT,
            entry_type=CatalogEntryType.SIGNAL,
        )
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": 'serial.matches("ITEM-[0-9]+")'},
        )
        submission = self._submission({"serial": "ITEM-123"})
        engine = BasicValidatorEngine()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 0)

    def test_output_stage_assertion(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            target_catalog_entry=self.output_signal,
            target_field="",
            rhs={"expr": "result.total == 5"},
        )
        self._submission({"price": 1})
        engine = BasicValidatorEngine()
        # Manually call assertion evaluation for output context
        result = engine.evaluate_assertions_for_stage(
            ruleset=self.ruleset,
            validator=self.validator,
            payload={"result": {"total": 5}},
            stage="output",
        )
        self.assertEqual(len(result.issues), 0)

    def test_startswith_helper_on_output(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            target_catalog_entry=self.output_status,
            target_field="",
            rhs={"expr": 'result.status.startsWith("OK")'},
        )
        engine = BasicValidatorEngine()
        result = engine.evaluate_assertions_for_stage(
            ruleset=self.ruleset,
            validator=self.validator,
            payload={"result": {"status": "OK_PASSED"}},
            stage="output",
        )
        self.assertEqual(len(result.issues), 0)

    def test_simple_string_prefix(self):
        ValidatorCatalogEntryFactory(
            validator=self.validator,
            slug="serial",
            run_stage=CatalogRunStage.INPUT,
            entry_type=CatalogEntryType.SIGNAL,
        )
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": 'serial.startsWith("ITEM-")'},
        )
        submission = self._submission({"serial": "ITEM-1234"})
        engine = BasicValidatorEngine()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)

    def test_simple_math(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "1 + 1 == 2"},
        )
        submission = self._submission({"price": 1})
        engine = BasicValidatorEngine()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)

    def test_list_index_and_map_access(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "items[0]['sku'] == 'A'"},
        )
        submission = self._submission({"items": [{"sku": "A"}, {"sku": "B"}]})
        engine = BasicValidatorEngine()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
