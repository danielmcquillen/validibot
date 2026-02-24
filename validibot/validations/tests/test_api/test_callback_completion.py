"""
Tests for container job callback completion behavior.

These tests cover correctness concerns beyond basic idempotency:

- When a validation run completes on an async callback, run/step summaries should
  be rebuilt for *all* steps (not just the final callback step).
- DO_NOT_STORE submission retention should be enforced without blocking the
  callback request path (purge is queued for the scheduled purge worker).
"""

import uuid
from unittest.mock import MagicMock
from unittest.mock import patch

from django.test import TestCase
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient
from validibot_shared.validations.envelopes import ValidationStatus

from validibot.submissions.models import PurgeRetry
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import ValidationRunSummary
from validibot.validations.tests.factories import ValidationFindingFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


class CallbackCompletionTestCase(TestCase):
    """
    Test callback behavior for runs that finish on an async validator callback.

    This is the common case for EnergyPlus/FMU: earlier steps may be synchronous,
    and the final step is async and finishes via /api/v1/validation-callbacks/.
    """

    def setUp(self):
        self.client = APIClient()
        self.callback_url = "/api/v1/validation-callbacks/"

        self.org = OrganizationFactory()
        self.user = UserFactory(orgs=[self.org])

        self.submission = SubmissionFactory(
            org=self.org,
            user=self.user,
        )

        self.run = ValidationRunFactory(
            submission=self.submission,
            status=ValidationRunStatus.RUNNING,
        )

        self.sync_validator = ValidatorFactory(
            validation_type=ValidationType.BASIC,
            is_system=True,
        )
        self.async_validator = ValidatorFactory(
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
        )

        self.step1 = WorkflowStepFactory(
            workflow=self.run.workflow,
            validator=self.sync_validator,
            order=10,
        )
        self.step2 = WorkflowStepFactory(
            workflow=self.run.workflow,
            validator=self.async_validator,
            order=20,
        )

        self.step_run1 = ValidationStepRunFactory(
            validation_run=self.run,
            workflow_step=self.step1,
            step_order=self.step1.order,
            status=StepStatus.PASSED,
        )
        self.step_run2 = ValidationStepRunFactory(
            validation_run=self.run,
            workflow_step=self.step2,
            step_order=self.step2.order,
            status=StepStatus.RUNNING,
            output={
                "job_name": "test-job",
                "execution_bundle_uri": "gs://bucket/runs/org/run",
            },
        )

        ValidationFindingFactory(
            validation_step_run=self.step_run1,
            severity=Severity.ERROR,
        )

    def _make_mock_envelope(self) -> MagicMock:
        """Create a mock output envelope for the async (callback) step."""
        msg = MagicMock()
        msg.severity = "WARNING"
        msg.code = "WARN001"
        msg.text = "Callback step produced a warning."
        msg.location = None
        msg.tags = []

        mock_envelope = MagicMock()
        mock_envelope.status = ValidationStatus.SUCCESS
        mock_envelope.validator = MagicMock()
        mock_envelope.validator.id = str(self.async_validator.id)
        mock_envelope.validator.version = "1.0.0"
        mock_envelope.run_id = str(self.run.id)
        mock_envelope.org = MagicMock()
        mock_envelope.org.id = str(self.org.id)
        mock_envelope.workflow = MagicMock()
        mock_envelope.workflow.step_id = str(self.step2.id)
        mock_envelope.timing = MagicMock()
        mock_envelope.timing.finished_at = None
        mock_envelope.messages = [msg]
        mock_envelope.outputs = MagicMock()
        mock_envelope.outputs.output_values = {}
        mock_envelope.model_dump.return_value = {
            "status": "success",
            "run_id": str(self.run.id),
        }
        return mock_envelope

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_callback_completion_rebuilds_summaries_and_queues_purge(
        self,
        mock_download,
    ):
        """
        Callback completion rebuilds summaries for all steps and queues DO_NOT_STORE
        purge.

        This protects correctness for multi-step workflows: a run may have persisted
        findings from earlier sync steps, then finish via an async callback.
        The callback handler must rebuild ValidationRunSummary/ValidationStepRunSummary
        for *all* steps so dashboards remain accurate.
        """
        mock_download.return_value = self._make_mock_envelope()

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
        self.assertEqual(response.data["message"], "Callback processed successfully")

        self.run.refresh_from_db()
        self.assertEqual(self.run.status, ValidationRunStatus.SUCCEEDED)

        summary_record = ValidationRunSummary.objects.get(run=self.run)
        self.assertEqual(summary_record.total_findings, 2)
        self.assertEqual(summary_record.error_count, 1)
        self.assertEqual(summary_record.warning_count, 1)
        self.assertEqual(summary_record.step_summaries.count(), 2)

        self.step_run2.refresh_from_db()
        self.assertEqual(
            self.step_run2.output.get("execution_bundle_uri"),
            "gs://bucket/runs/org/run",
        )
        self.assertEqual(self.step_run2.output.get("status"), "success")

        self.submission.refresh_from_db()
        self.assertEqual(self.submission.content, "{}")
        self.assertTrue(PurgeRetry.objects.filter(submission=self.submission).exists())
