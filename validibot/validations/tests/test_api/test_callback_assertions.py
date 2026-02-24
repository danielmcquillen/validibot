"""
Tests for assertion evaluation during container job callback processing.

After an async validator (EnergyPlus, FMU) completes, the callback service
evaluates output-stage assertions against the envelope outputs. This includes
generating success messages for passed assertions.

These tests verify that:
1. Output-stage assertions are evaluated when the callback is received
2. Success messages are generated for passed assertions
3. Failure messages are generated for failed assertions
"""

import uuid
from unittest.mock import MagicMock
from unittest.mock import patch

from django.test import TestCase
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient

from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import ValidationFinding
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorCatalogEntryFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


class CallbackAssertionEvaluationTests(TestCase):
    """
    Tests for output-stage assertion evaluation during callback processing.

    Verifies that CEL assertions targeting output-stage catalog entries are
    evaluated after an async validator completes and returns its output envelope.
    """

    def setUp(self):
        self.client = APIClient()
        self.callback_url = "/api/v1/validation-callbacks/"

        self.org = OrganizationFactory()
        self.user = UserFactory(orgs=[self.org])

        # Create EnergyPlus validator with output catalog entry
        self.validator = ValidatorFactory(
            org=self.org,
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
        )
        self.output_entry = ValidatorCatalogEntryFactory(
            validator=self.validator,
            slug="site_eui_kwh_m2",
            label="Site EUI (kWh/m²)",
            run_stage=CatalogRunStage.OUTPUT,
        )

        # Create ruleset with CEL assertion
        self.ruleset = RulesetFactory(org=self.org)

        # Create workflow step with show_success_messages enabled
        self.run = ValidationRunFactory(
            org=self.org,
            status=ValidationRunStatus.RUNNING,
        )
        self.step = WorkflowStepFactory(
            workflow=self.run.workflow,
            validator=self.validator,
            ruleset=self.ruleset,
            order=10,
            show_success_messages=True,
        )
        self.step_run = ValidationStepRunFactory(
            validation_run=self.run,
            workflow_step=self.step,
            step_order=self.step.order,
            status=StepStatus.RUNNING,
        )

    def _make_mock_envelope(
        self,
        *,
        metrics: dict | None = None,
        messages: list | None = None,
    ) -> MagicMock:
        """
        Create a mock EnergyPlus output envelope for the async validator callback.

        Args:
            metrics: Dict of output metrics keyed by field name (e.g., site_eui_kwh_m2).
                    This matches EnergyPlus's outputs.metrics structure.
            messages: Optional list of envelope message objects.
        """
        from validibot_shared.validations.envelopes import ValidationStatus

        # Create a simple class to hold outputs that won't auto-generate mocks
        class MockMetrics:
            def model_dump(self, mode=None):
                return metrics or {}

        class MockOutputs:
            def __init__(self):
                self.metrics = MockMetrics()
                self.output_values = None
                self.outputs = None  # Explicitly set to prevent auto-mock

            def model_dump(self, mode=None):
                return {"metrics": metrics or {}}

        class MockTiming:
            finished_at = None

        class MockValidator:
            def __init__(self, validator_id):
                self.id = validator_id
                self.version = "1.0.0"

        mock_envelope = MagicMock()
        mock_envelope.status = ValidationStatus.SUCCESS
        mock_envelope.validator = MockValidator(str(self.validator.id))
        mock_envelope.run_id = str(self.run.id)
        mock_envelope.timing = MockTiming()
        mock_envelope.messages = messages or []
        mock_envelope.outputs = MockOutputs()
        # Return JSON-serializable dict - this is what gets stored in step_run.output
        mock_envelope.model_dump.return_value = {
            "status": "success",
            "run_id": str(self.run.id),
            "outputs": {"metrics": metrics or {}},
        }
        return mock_envelope

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_callback_evaluates_output_assertions_and_creates_success_finding(
        self,
        mock_download,
    ):
        """
        Callback evaluates output assertions and creates success findings.

        When a CEL assertion passes and show_success_messages is enabled,
        the callback should create a SUCCESS severity finding.
        """
        # Create assertion that will pass (site_eui < 100 when value is 75)
        assertion = RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type="cel_expr",
            target_catalog_entry=self.output_entry,
            target_field="",  # Required: clear target_field when using catalog entry
            rhs={"expr": "site_eui_kwh_m2 < 100"},
            success_message="Building meets energy efficiency target!",
        )

        mock_download.return_value = self._make_mock_envelope(
            metrics={"site_eui_kwh_m2": 75},
        )

        callback_id = str(uuid.uuid4())
        response = self.client.post(
            self.callback_url,
            data={
                "run_id": str(self.run.id),
                "callback_id": callback_id,
                "status": "success",
                "result_uri": "gs://bucket/runs/output.json",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify success finding was created
        success_findings = ValidationFinding.objects.filter(
            validation_step_run=self.step_run,
            severity=Severity.SUCCESS,
        )
        self.assertEqual(success_findings.count(), 1)
        finding = success_findings.first()
        self.assertEqual(finding.code, "assertion_passed")
        self.assertEqual(finding.message, "Building meets energy efficiency target!")
        self.assertEqual(finding.ruleset_assertion_id, assertion.id)

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_callback_evaluates_output_assertions_and_creates_failure_finding(
        self,
        mock_download,
    ):
        """
        Callback evaluates output assertions and creates failure findings.

        When a CEL assertion fails, the callback should create an ERROR
        severity finding with the failure message.
        """
        # Create assertion that will fail (site_eui < 50 when value is 75)
        assertion = RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type="cel_expr",
            target_catalog_entry=self.output_entry,
            target_field="",  # Required: clear target_field when using catalog entry
            rhs={"expr": "site_eui_kwh_m2 < 50"},
            message_template="Site EUI is too high!",
        )

        mock_download.return_value = self._make_mock_envelope(
            metrics={"site_eui_kwh_m2": 75},
        )

        callback_id = str(uuid.uuid4())
        response = self.client.post(
            self.callback_url,
            data={
                "run_id": str(self.run.id),
                "callback_id": callback_id,
                "status": "success",
                "result_uri": "gs://bucket/runs/output.json",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify failure finding was created
        error_findings = ValidationFinding.objects.filter(
            validation_step_run=self.step_run,
            severity=Severity.ERROR,
        )
        self.assertEqual(error_findings.count(), 1)
        finding = error_findings.first()
        self.assertEqual(finding.message, "Site EUI is too high!")
        self.assertEqual(finding.ruleset_assertion_id, assertion.id)

        # Step should be marked failed when an output-stage assertion fails
        self.step_run.refresh_from_db()
        self.assertEqual(self.step_run.status, StepStatus.FAILED)

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_callback_success_with_container_errors_adds_warning_but_passes(
        self,
        mock_download,
    ):
        """
        SUCCESS status with container ERROR messages should still pass the step,
        but emit a warning finding.
        """
        from validibot_shared.validations.envelopes import Severity as EnvelopeSeverity

        mock_message = MagicMock()
        mock_message.text = "Container reported an error"
        mock_message.severity = EnvelopeSeverity.ERROR
        mock_message.location = None

        mock_download.return_value = self._make_mock_envelope(
            metrics={"site_eui_kwh_m2": 75},
            messages=[mock_message],
        )

        callback_id = str(uuid.uuid4())
        response = self.client.post(
            self.callback_url,
            data={
                "run_id": str(self.run.id),
                "callback_id": callback_id,
                "status": "success",
                "result_uri": "gs://bucket/runs/output.json",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.step_run.refresh_from_db()
        self.assertEqual(self.step_run.status, StepStatus.PASSED)

        warning_findings = ValidationFinding.objects.filter(
            validation_step_run=self.step_run,
            severity=Severity.WARNING,
            code="advanced_validation_success_with_errors",
        )
        self.assertEqual(warning_findings.count(), 1)

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_callback_no_success_finding_without_show_success_messages(
        self,
        mock_download,
    ):
        """
        No success findings when show_success_messages is disabled.

        When a CEL assertion passes but show_success_messages is False
        and no custom success_message is set, no finding should be created.
        """
        # Disable show_success_messages on step
        self.step.show_success_messages = False
        self.step.save(update_fields=["show_success_messages"])

        # Create assertion without custom success message
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type="cel_expr",
            target_catalog_entry=self.output_entry,
            target_field="",  # Required: clear target_field when using catalog entry
            rhs={"expr": "site_eui_kwh_m2 < 100"},
            success_message="",  # No custom message
        )

        mock_download.return_value = self._make_mock_envelope(
            metrics={"site_eui_kwh_m2": 75},
        )

        callback_id = str(uuid.uuid4())
        response = self.client.post(
            self.callback_url,
            data={
                "run_id": str(self.run.id),
                "callback_id": callback_id,
                "status": "success",
                "result_uri": "gs://bucket/runs/output.json",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # No findings should be created (passed silently)
        findings = ValidationFinding.objects.filter(
            validation_step_run=self.step_run,
        )
        self.assertEqual(findings.count(), 0)

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_callback_custom_success_message_overrides_default(
        self,
        mock_download,
    ):
        """
        Custom success message is used when assertion has one defined.

        When an assertion has a success_message set, it should be used
        for the success finding even without show_success_messages enabled.
        """
        # Disable show_success_messages on step
        self.step.show_success_messages = False
        self.step.save(update_fields=["show_success_messages"])

        # Create assertion WITH custom success message
        RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type="cel_expr",
            target_catalog_entry=self.output_entry,
            target_field="",  # Required: clear target_field when using catalog entry
            rhs={"expr": "site_eui_kwh_m2 < 100"},
            success_message="Custom: Building is energy efficient!",
        )

        mock_download.return_value = self._make_mock_envelope(
            metrics={"site_eui_kwh_m2": 75},
        )

        callback_id = str(uuid.uuid4())
        response = self.client.post(
            self.callback_url,
            data={
                "run_id": str(self.run.id),
                "callback_id": callback_id,
                "status": "success",
                "result_uri": "gs://bucket/runs/output.json",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Success finding should be created with custom message
        success_findings = ValidationFinding.objects.filter(
            validation_step_run=self.step_run,
            severity=Severity.SUCCESS,
        )
        self.assertEqual(success_findings.count(), 1)
        finding = success_findings.first()
        self.assertEqual(
            finding.message,
            "Custom: Building is energy efficient!",
        )

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_callback_evaluates_cel_assertion_without_target_catalog_entry(
        self,
        mock_download,
    ):
        """
        CEL assertions without target_catalog_entry should still evaluate.

        This tests the real-world scenario where the assertion form sets
        target_catalog_entry=None for CEL expression assertions. The stage
        should be inferred from which signals the expression references.
        """
        # Create assertion WITHOUT target_catalog_entry (mimics form behavior)
        # but referencing an output-stage signal.
        # Note: DB constraint requires target_field to be non-empty when
        # target_catalog_entry is null - the form sets it to the CEL expression.
        assertion = RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type="cel_expr",
            target_catalog_entry=None,  # Form sets this to None for CEL assertions
            target_field="site_eui_kwh_m2 < 100",  # Form sets this to the expression
            rhs={"expr": "site_eui_kwh_m2 < 100"},
            success_message="Building meets energy efficiency target!",
        )

        mock_download.return_value = self._make_mock_envelope(
            metrics={"site_eui_kwh_m2": 75},
        )

        callback_id = str(uuid.uuid4())
        response = self.client.post(
            self.callback_url,
            data={
                "run_id": str(self.run.id),
                "callback_id": callback_id,
                "status": "success",
                "result_uri": "gs://bucket/runs/output.json",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Success finding should be created even without target_catalog_entry
        success_findings = ValidationFinding.objects.filter(
            validation_step_run=self.step_run,
            severity=Severity.SUCCESS,
        )
        self.assertEqual(
            success_findings.count(),
            1,
            f"Expected 1 SUCCESS finding but found {success_findings.count()}. "
            "The assertion stage should be inferred from the expression.",
        )
        finding = success_findings.first()
        self.assertEqual(finding.code, "assertion_passed")
        self.assertEqual(finding.message, "Building meets energy efficiency target!")
        self.assertEqual(finding.ruleset_assertion_id, assertion.id)
