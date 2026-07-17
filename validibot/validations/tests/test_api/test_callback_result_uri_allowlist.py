"""
Tests for the worker callback ``result_uri`` GCS allowlist (SSRF / arbitrary read).

The container-job callback endpoint accepts a ``result_uri`` string from the
(OIDC-authenticated) validator container and then downloads + trusts that object
as the run's output envelope. ``result_uri`` is fully container-controlled, so
without a server-side allowlist a compromised or buggy container could point it
at ANY object the worker service account can read — cross-org outputs, secret
bundles, unrelated buckets — turning the worker into an arbitrary-GCS-read /
result-substitution primitive.

``ValidationCallbackService._validate_result_uri_allowlist`` closes that gap:
when ``GCS_VALIDATION_BUCKET`` is configured (the real async/GCS path), the
``result_uri`` must be a ``gs://`` URI in that bucket and under the run's
deterministic prefix ``runs/<org_id>/<run_id>/`` that the launcher writes.

These tests matter because they pin the security boundary directly at the HTTP
edge: a mismatching URI must be rejected *before* ``download_envelope`` is ever
called (no GCS access on a hostile path), while the legitimate in-prefix URI
must still flow through normally so we don't break real EnergyPlus/FMU callbacks.
"""

from unittest.mock import MagicMock
from unittest.mock import patch

from django.test import TestCase
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient
from validibot_shared.validations.envelopes import ValidationStatus

from validibot.submissions.tests.factories import SubmissionFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.services.execution_attempts import build_attempt_callback_id
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

# Bucket the deployment is configured to read run bundles from. The allowlist
# pins callback result URIs to this bucket; anything else must be rejected.
ALLOWED_BUCKET = "validibot-validation-bucket"


@override_settings(
    APP_IS_WORKER=True,
    ROOT_URLCONF="config.urls_worker",
    GCS_VALIDATION_BUCKET=ALLOWED_BUCKET,
)
class CallbackResultUriAllowlistTestCase(TestCase):
    """
    Verify the callback handler pins ``result_uri`` to the run's own GCS prefix.

    We build a single async (EnergyPlus) step in RUNNING state — the exact shape
    that finishes via ``/api/v1/validation-callbacks/`` — and then exercise both
    a hostile and a legitimate ``result_uri`` against it.
    """

    def setUp(self):
        self.client = APIClient()
        self.callback_url = "/api/v1/validation-callbacks/"

        self.org = OrganizationFactory()
        self.user = UserFactory(orgs=[self.org])
        self.submission = SubmissionFactory(org=self.org, user=self.user)

        self.run = ValidationRunFactory(
            submission=self.submission,
            status=ValidationRunStatus.RUNNING,
        )

        self.async_validator = ValidatorFactory(
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
        )
        self.step = WorkflowStepFactory(
            workflow=self.run.workflow,
            validator=self.async_validator,
            order=10,
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
        )
        self.callback_id = build_attempt_callback_id(self.attempt)

        # The deterministic, per-run prefix the launcher writes bundles under.
        self.run_prefix = f"runs/{self.run.org_id}/{self.run.id}"

    def _make_mock_envelope(self) -> MagicMock:
        """Build a minimal valid output envelope for the happy-path call."""
        mock_envelope = MagicMock()
        mock_envelope.status = ValidationStatus.SUCCESS
        mock_envelope.validator = MagicMock()
        mock_envelope.validator.id = str(self.async_validator.id)
        mock_envelope.validator.type = ValidationType.ENERGYPLUS
        mock_envelope.run_id = str(self.run.id)
        mock_envelope.step_run_id = str(self.step_run.pk)
        mock_envelope.execution_attempt_id = str(self.attempt.pk)
        mock_envelope.attempt_contract_version = "validibot.attempt.v1"
        mock_envelope.input_envelope_sha256 = self.attempt.input_envelope_sha256
        mock_envelope.output_uri = self.attempt.output_envelope_uri
        mock_envelope.timing = MagicMock()
        mock_envelope.timing.finished_at = None
        mock_envelope.messages = []
        mock_envelope.outputs = MagicMock()
        mock_envelope.outputs.output_values = {}
        mock_envelope.model_dump.return_value = {"status": "success"}
        return mock_envelope

    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_result_uri_outside_run_prefix_is_rejected_without_gcs_access(
        self,
        mock_download,
    ):
        """
        A ``result_uri`` in a different bucket/prefix must be rejected pre-download.

        This is the core security assertion: pointing ``result_uri`` at an
        attacker-chosen bucket (here a victim's bucket) must return HTTP 400 and,
        critically, must NOT call ``download_envelope`` — otherwise the worker
        would have already issued an arbitrary GCS read on its service account.
        """
        hostile_uri = "gs://some-other-victim-bucket/runs/evil/output.json"
        self.attempt.output_envelope_uri = hostile_uri
        self.attempt.save(update_fields=["output_envelope_uri"])

        response = self.client.post(
            self.callback_url,
            data={
                "run_id": str(self.run.id),
                "callback_id": self.callback_id,
                "status": "success",
                "result_uri": hostile_uri,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # The hostile object was never fetched — the allowlist short-circuited.
        mock_download.assert_not_called()
        # The run was not advanced to a terminal state off a forged URI.
        self.run.refresh_from_db()
        self.assertEqual(self.run.status, ValidationRunStatus.RUNNING)

    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_result_uri_within_run_prefix_is_accepted(self, mock_download):
        """
        A ``result_uri`` in the configured bucket under the run's own prefix passes.

        This guards against an over-eager allowlist breaking real EnergyPlus/FMU
        callbacks: the legitimate per-run path must still flow through to
        ``download_envelope`` and complete the run normally.
        """
        mock_download.return_value = self._make_mock_envelope()
        legit_uri = f"gs://{ALLOWED_BUCKET}/{self.run_prefix}/output.json"
        self.attempt.output_envelope_uri = legit_uri
        self.attempt.save(update_fields=["output_envelope_uri"])
        mock_download.return_value.output_uri = legit_uri

        response = self.client.post(
            self.callback_url,
            data={
                "run_id": str(self.run.id),
                "callback_id": self.callback_id,
                "status": "success",
                "result_uri": legit_uri,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        mock_download.assert_called_once()
        # The legitimate URI is exactly what was handed to the downloader.
        self.assertEqual(mock_download.call_args.args[0], legit_uri)
