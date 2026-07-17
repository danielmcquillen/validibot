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

from unittest.mock import MagicMock
from unittest.mock import patch

from django.test import TestCase
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient

from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import ValidationFinding
from validibot.validations.services.execution_attempts import build_attempt_callback_id
from validibot.validations.services.execution_attempts import (
    build_callback_nonce_verifier,
)
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import RulesetAssertionFactory
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

TEST_CALLBACK_NONCE = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"


class CallbackAssertionEvaluationTests(TestCase):
    """
    Tests for output-stage assertion evaluation during callback processing.

    Verifies that CEL assertions targeting output-stage step I/O definitions are
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
        self.output_definition = StepIODefinitionFactory(
            validator=self.validator,
            contract_key="site_eui_kwh_m2",
            label="Site EUI (kWh/m²)",
            direction="output",
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
        self.attempt = ExecutionAttemptFactory(
            step_run=self.step_run,
            state="RUNNING",
            callback_nonce_hash=build_callback_nonce_verifier(
                TEST_CALLBACK_NONCE,
            ),
            output_envelope_uri="gs://bucket/runs/output.json",
        )
        self.callback_id = build_attempt_callback_id(self.attempt)

    def _make_mock_envelope(
        self,
        *,
        metrics: dict | None = None,
        messages: list | None = None,
        validator_id: str | None = None,
        run_id: str | None = None,
        attempt=None,
    ) -> MagicMock:
        """
        Create a mock EnergyPlus output envelope for the async validator callback.

        Args:
            metrics: Dict of output metrics keyed by field name (e.g., site_eui_kwh_m2).
                    This matches EnergyPlus's outputs.metrics structure.
            messages: Optional list of envelope message objects.
            validator_id: Envelope's reported validator id. Defaults to the
                    setUp validator; pass a different id when the resolved step
                    uses another validator, or the callback's validator-match
                    check (envelope vs expected) rejects the callback with 400.
            run_id: Envelope's reported run id. Defaults to the setUp run.
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
                self.type = ValidationType.ENERGYPLUS
                self.version = "1.0.0"

        resolved_validator_id = (
            str(validator_id) if validator_id is not None else str(self.validator.id)
        )
        resolved_run_id = str(run_id) if run_id is not None else str(self.run.id)
        resolved_attempt = attempt or self.attempt

        mock_envelope = MagicMock()
        mock_envelope.status = ValidationStatus.SUCCESS
        mock_envelope.validator = MockValidator(resolved_validator_id)
        mock_envelope.run_id = resolved_run_id
        mock_envelope.step_run_id = str(resolved_attempt.step_run_id)
        mock_envelope.execution_attempt_id = str(resolved_attempt.pk)
        mock_envelope.attempt_contract_version = "validibot.attempt.v2"
        mock_envelope.input_envelope_sha256 = resolved_attempt.input_envelope_sha256
        mock_envelope.output_uri = resolved_attempt.output_envelope_uri
        mock_envelope.timing = MockTiming()
        mock_envelope.messages = messages or []
        mock_envelope.outputs = MockOutputs()
        # Return JSON-serializable dict - this is what gets stored in step_run.output
        mock_envelope.model_dump.return_value = {
            "status": "success",
            "run_id": resolved_run_id,
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
            target_io_definition=self.output_definition,
            target_data_path="",  # Must be empty when using step I/O definition
            rhs={"expr": "output.site_eui_kwh_m2 < 100"},
            success_message="Building meets energy efficiency target!",
        )

        mock_download.return_value = self._make_mock_envelope(
            metrics={"site_eui_kwh_m2": 75},
        )

        callback_id = self.callback_id
        response = self.client.post(
            self.callback_url,
            data={
                "run_id": str(self.run.id),
                "callback_id": callback_id,
                "callback_nonce": TEST_CALLBACK_NONCE,
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
            target_io_definition=self.output_definition,
            target_data_path="",  # Must be empty when using step I/O definition
            rhs={"expr": "output.site_eui_kwh_m2 < 50"},
            message_template="Site EUI is too high!",
        )

        mock_download.return_value = self._make_mock_envelope(
            metrics={"site_eui_kwh_m2": 75},
        )

        callback_id = self.callback_id
        response = self.client.post(
            self.callback_url,
            data={
                "run_id": str(self.run.id),
                "callback_id": callback_id,
                "callback_nonce": TEST_CALLBACK_NONCE,
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
    def test_callback_success_with_container_errors_fails_for_custom_validator(
        self,
        mock_download,
    ):
        """A custom container reporting SUCCESS with ERROR findings must FAIL.

        Why this matters: a user-added (custom) validator is not trusted to
        honour the envelope-status contract. A buggy or naive container can set
        status=SUCCESS ("my container ran") while emitting ERROR-severity
        findings ("the data is invalid"). For a validation + attestation
        product we must not PASS the step — and let a signed credential be
        issued — over data the validator itself flagged as an ERROR. So the
        ERROR findings win and the step is FAILED.

        ``self.validator`` is org-owned, and ``Validator.save()`` forces
        ``is_system=False`` whenever an org is set, so this fixture is a genuine
        *custom* validator (asserted below to guard against the fixture
        silently becoming a system validator and masking a regression).
        """
        from validibot_shared.validations.envelopes import Severity as EnvelopeSeverity

        # Guard: the shared fixture must be a custom (non-system) validator for
        # this test to exercise the untrusted-container path.
        self.assertFalse(self.validator.is_system)

        mock_message = MagicMock()
        mock_message.text = "Container reported an error"
        mock_message.severity = EnvelopeSeverity.ERROR
        mock_message.location = None

        mock_download.return_value = self._make_mock_envelope(
            metrics={"site_eui_kwh_m2": 75},
            messages=[mock_message],
        )

        callback_id = self.callback_id
        response = self.client.post(
            self.callback_url,
            data={
                "run_id": str(self.run.id),
                "callback_id": callback_id,
                "callback_nonce": TEST_CALLBACK_NONCE,
                "status": "success",
                "result_uri": "gs://bucket/runs/output.json",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Custom container + SUCCESS-with-ERROR => the step FAILS.
        self.step_run.refresh_from_db()
        self.assertEqual(self.step_run.status, StepStatus.FAILED)

        # An ERROR-severity finding explains *why* the step failed (so the
        # reason is visible in the run's findings, not just the status).
        error_findings = ValidationFinding.objects.filter(
            validation_step_run=self.step_run,
            severity=Severity.ERROR,
            code="advanced_validation_custom_success_with_errors",
        )
        self.assertEqual(error_findings.count(), 1)

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_callback_success_with_container_errors_passes_for_system_validator(
        self,
        mock_download,
    ):
        """A SHIPPED validator reporting SUCCESS with ERROR findings still PASSES.

        Why this matters: shipped (``is_system=True``) validators are
        authoritative about pass/fail via their envelope status. EnergyPlus
        legitimately exits 0 (SUCCESS) while writing ``** Severe **``
        ERROR-severity lines it considers non-fatal — so a naive "any ERROR
        fails" rule would break it. We keep the step PASSED and surface a
        WARNING finding. This is the ``is_system`` carve-out that distinguishes
        trusted shipped validators from untrusted custom containers (the
        failing-custom counterpart is the test directly above).

        The fixture is built with ``org=None`` so ``Validator.save()`` leaves
        ``is_system=True`` (org-owned validators are forced to custom), and runs
        in its own dedicated run/step chain so the callback's "first active
        step" resolution lands on the system step unambiguously.
        """
        from validibot_shared.validations.envelopes import Severity as EnvelopeSeverity

        # A genuine system validator: org=None means save() does not flip it.
        system_validator = ValidatorFactory(
            org=None,
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
        )
        self.assertTrue(system_validator.is_system)
        StepIODefinitionFactory(
            validator=system_validator,
            contract_key="site_eui_kwh_m2",
            label="Site EUI (kWh/m²)",
            direction="output",
        )

        system_run = ValidationRunFactory(
            org=self.org,
            status=ValidationRunStatus.RUNNING,
        )
        system_step = WorkflowStepFactory(
            workflow=system_run.workflow,
            validator=system_validator,
            order=10,
            show_success_messages=True,
        )
        system_step_run = ValidationStepRunFactory(
            validation_run=system_run,
            workflow_step=system_step,
            step_order=system_step.order,
            status=StepStatus.RUNNING,
        )
        system_attempt = ExecutionAttemptFactory(
            step_run=system_step_run,
            state="RUNNING",
            callback_nonce_hash=build_callback_nonce_verifier(
                TEST_CALLBACK_NONCE,
            ),
            output_envelope_uri="gs://bucket/runs/output.json",
        )

        mock_message = MagicMock()
        mock_message.text = "** Severe ** non-fatal EnergyPlus message"
        mock_message.severity = EnvelopeSeverity.ERROR
        mock_message.location = None

        mock_download.return_value = self._make_mock_envelope(
            metrics={"site_eui_kwh_m2": 75},
            messages=[mock_message],
            validator_id=system_validator.id,
            run_id=system_run.id,
            attempt=system_attempt,
        )

        callback_id = build_attempt_callback_id(system_attempt)
        response = self.client.post(
            self.callback_url,
            data={
                "run_id": str(system_run.id),
                "callback_id": callback_id,
                "callback_nonce": TEST_CALLBACK_NONCE,
                "status": "success",
                "result_uri": "gs://bucket/runs/output.json",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Shipped validator => trusted => step PASSES despite the ERROR message.
        system_step_run.refresh_from_db()
        self.assertEqual(system_step_run.status, StepStatus.PASSED)

        # A WARNING (not ERROR) finding surfaces the SUCCESS-with-errors note.
        warning_findings = ValidationFinding.objects.filter(
            validation_step_run=system_step_run,
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
            target_io_definition=self.output_definition,
            target_data_path="",  # Must be empty when using step I/O definition
            rhs={"expr": "output.site_eui_kwh_m2 < 100"},
            success_message="",  # No custom message
        )

        mock_download.return_value = self._make_mock_envelope(
            metrics={"site_eui_kwh_m2": 75},
        )

        callback_id = self.callback_id
        response = self.client.post(
            self.callback_url,
            data={
                "run_id": str(self.run.id),
                "callback_id": callback_id,
                "callback_nonce": TEST_CALLBACK_NONCE,
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
            target_io_definition=self.output_definition,
            target_data_path="",  # Must be empty when using step I/O definition
            rhs={"expr": "output.site_eui_kwh_m2 < 100"},
            success_message="Custom: Building is energy efficient!",
        )

        mock_download.return_value = self._make_mock_envelope(
            metrics={"site_eui_kwh_m2": 75},
        )

        callback_id = self.callback_id
        response = self.client.post(
            self.callback_url,
            data={
                "run_id": str(self.run.id),
                "callback_id": callback_id,
                "callback_nonce": TEST_CALLBACK_NONCE,
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
    def test_callback_evaluates_cel_assertion_without_target_io_definition(
        self,
        mock_download,
    ):
        """
        CEL assertions without target_io_definition should still evaluate.

        This tests the real-world scenario where the assertion form sets
        target_io_definition=None for CEL expression assertions. The stage
        should be inferred from which step-I/O namespace the expression uses.
        """
        # Create assertion WITHOUT target_io_definition (mimics form behavior)
        # but referencing a step output.
        # Note: DB constraint requires target_data_path to be non-empty when
        # target_io_definition is null - the form sets it to the CEL expression.
        assertion = RulesetAssertionFactory(
            ruleset=self.ruleset,
            assertion_type="cel_expr",
            target_io_definition=None,  # Form sets this to None for CEL assertions
            # For CEL assertions, the expression is stored as the data path.
            target_data_path="output.site_eui_kwh_m2 < 100",
            rhs={"expr": "output.site_eui_kwh_m2 < 100"},
            success_message="Building meets energy efficiency target!",
        )

        mock_download.return_value = self._make_mock_envelope(
            metrics={"site_eui_kwh_m2": 75},
        )

        callback_id = self.callback_id
        response = self.client.post(
            self.callback_url,
            data={
                "run_id": str(self.run.id),
                "callback_id": callback_id,
                "callback_nonce": TEST_CALLBACK_NONCE,
                "status": "success",
                "result_uri": "gs://bucket/runs/output.json",
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Success finding should be created even without target_io_definition
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
