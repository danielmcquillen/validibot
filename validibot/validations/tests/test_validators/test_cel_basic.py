"""Tests for CEL expression evaluation in the BasicValidator.

Covers input-signal, output-signal, derived-signal, and literal CEL
expressions, including helper functions (size, matches, startsWith),
when-guard skipping, invalid-expression error reporting, dotted-slug
resolution, and missing-signal null handling.

CEL expressions reference signals via the ``s`` (signals) namespace
(e.g. ``p.price < 10``).  Raw payload data that is not backed by a
signal definition would use the ``p`` (payload) namespace instead.
The ``output`` and ``steps`` namespaces are available for validator
outputs and upstream step outputs respectively.
"""

from __future__ import annotations

import json

from django.test import TestCase
from django.test import override_settings

from validibot.projects.tests.factories import ProjectFactory
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import DerivationFactory
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import SignalDefinitionFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.basic import BasicValidator


@override_settings(ENABLE_DERIVED_SIGNALS=True)
class CelBasicValidatorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = OrganizationFactory()
        cls.validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=False,
            org=cls.org,
        )
        # Signal definitions spanning input/output/derived
        cls.input_signal = SignalDefinitionFactory(
            validator=cls.validator,
            contract_key="price",
            direction="input",
        )
        cls.output_signal = SignalDefinitionFactory(
            validator=cls.validator,
            contract_key="result.total",
            direction="output",
        )
        cls.output_status = SignalDefinitionFactory(
            validator=cls.validator,
            contract_key="result.status",
            direction="output",
        )
        cls.derived_signal = DerivationFactory(
            validator=cls.validator,
            contract_key="metrics.avg",
        )
        cls.required_entry = SignalDefinitionFactory(
            validator=cls.validator,
            contract_key="required_value",
            direction="input",
        )
        cls.list_signal = SignalDefinitionFactory(
            validator=cls.validator,
            contract_key="items",
            direction="input",
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
            rhs={"expr": "p.price < 10"},
        )
        submission = self._submission({"price": 5})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 0)

    def test_list_size_helper(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "size(p.items) == 2"},
        )
        submission = self._submission({"items": [{"sku": "A"}, {"sku": "B"}]})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 0)

    def test_false_expression_on_input_signal(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "p.price < 10"},
            severity=Severity.ERROR,
        )
        submission = self._submission({"price": 25})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("Peak too high", result.issues[0].message)

    def test_when_guard_skips_expression(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "p.price < 10"},
            when_expression="p.price > 100",
        )
        submission = self._submission({"price": 20})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 0)

    def test_invalid_expression_reports_error(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "p.price < "},  # invalid CEL
        )
        submission = self._submission({"price": 5})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("CEL evaluation failed", result.issues[0].message)

    def test_dotted_slug_resolution(self):
        """Dotted path access on the payload namespace should resolve
        nested values correctly. ``p.metrics.avg`` navigates into the
        raw payload dict via CEL's native dot-access on MapType.
        """
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "p.metrics.avg == 3"},
        )
        submission = self._submission({"metrics": {"avg": 3}})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 0)

    def test_missing_payload_key_produces_evaluation_error(self):
        """Accessing a missing payload key via ``p.missing_key`` produces
        a CEL evaluation error (field not found).  Under the namespaced
        design, there is no implicit None injection for missing keys —
        the author must either define a signal with ``on_missing=null``
        or guard with ``has(p.missing_key)``.
        """
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "p.required_value == null"},
        )
        submission = self._submission({"price": 5})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertFalse(result.passed)
        self.assertEqual(len(result.issues), 1)
        self.assertIn("CEL evaluation failed", result.issues[0].message)

    def test_matches_helper_on_input(self):
        slug = "serial"
        SignalDefinitionFactory(
            validator=self.validator,
            contract_key=slug,
            direction="input",
        )
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": 'p.serial.matches("ITEM-[0-9]+")'},
        )
        submission = self._submission({"serial": "ITEM-123"})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
        self.assertEqual(len(result.issues), 0)

    def test_output_stage_assertion(self):
        output_sig = self.output_signal
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            target_signal_definition=output_sig,
            target_data_path="",
            rhs={"expr": "output.result.total == 5"},
        )
        self._submission({"price": 1})
        engine = BasicValidator()
        # Manually call assertion evaluation for output context
        result = engine.evaluate_assertions_for_stage(
            ruleset=self.ruleset,
            validator=self.validator,
            payload={"result": {"total": 5}},
            stage="output",
        )
        self.assertEqual(len(result.issues), 0)

    def test_startswith_helper_on_output(self):
        status_sig = self.output_status
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            target_signal_definition=status_sig,
            target_data_path="",
            rhs={"expr": 'output.result.status.startsWith("OK")'},
        )
        engine = BasicValidator()
        result = engine.evaluate_assertions_for_stage(
            ruleset=self.ruleset,
            validator=self.validator,
            payload={"result": {"status": "OK_PASSED"}},
            stage="output",
        )
        self.assertEqual(len(result.issues), 0)

    def test_simple_string_prefix(self):
        SignalDefinitionFactory(
            validator=self.validator,
            contract_key="serial",
            direction="input",
        )
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": 'p.serial.startsWith("ITEM-")'},
        )
        submission = self._submission({"serial": "ITEM-1234"})
        engine = BasicValidator()

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
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)

    def test_list_index_and_map_access(self):
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            operator=AssertionOperator.CEL_EXPR,
            rhs={"expr": "p.items[0]['sku'] == 'A'"},
        )
        submission = self._submission({"items": [{"sku": "A"}, {"sku": "B"}]})
        engine = BasicValidator()

        result = engine.validate(self.validator, submission, self.ruleset)

        self.assertTrue(result.passed)
