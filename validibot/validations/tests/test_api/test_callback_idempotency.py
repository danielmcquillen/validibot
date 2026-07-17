"""
Tests for container job callback idempotency.

These tests verify that duplicate callback deliveries from container jobs are
handled correctly - the first callback is processed, subsequent callbacks
with the same callback_id are ignored (return 200 without reprocessing).
"""

import uuid
from unittest.mock import MagicMock
from unittest.mock import patch

from django.test import TestCase
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient
from validibot_shared.validations.envelopes import ValidationStatus

from validibot.core.models import CallbackReceiptStatus
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import CallbackReceipt
from validibot.validations.services.execution_attempts import build_attempt_callback_id
from validibot.validations.services.execution_attempts import (
    build_callback_nonce_verifier,
)
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

TEST_CALLBACK_NONCE = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
WRONG_CALLBACK_NONCE = "Hh0cGxoZGBcWFRQTEhEQDw4NDAsKCQgHBgUEAwIBAAA"


class CallbackIdempotencyTestCase(TestCase):
    """Test callback idempotency for container job callbacks."""

    def setUp(self):
        self.client = APIClient()
        self.org = OrganizationFactory()
        self.user = UserFactory(orgs=[self.org])

        # Create a validator
        self.validator = ValidatorFactory(
            validation_type=ValidationType.ENERGYPLUS,
            is_system=True,
        )

        # Create a validation run in RUNNING state
        self.run = ValidationRunFactory(
            org=self.org,
            user=self.user,
            status=ValidationRunStatus.RUNNING,
        )

        # Create a workflow step with the validator
        self.workflow_step = WorkflowStepFactory(
            workflow=self.run.workflow,
            validator=self.validator,
        )

        # Create a step run in RUNNING state
        self.step_run = ValidationStepRunFactory(
            validation_run=self.run,
            workflow_step=self.workflow_step,
            status=StepStatus.RUNNING,
        )
        self.attempt = ExecutionAttemptFactory(
            step_run=self.step_run,
            state="RUNNING",
            callback_nonce_hash=build_callback_nonce_verifier(
                TEST_CALLBACK_NONCE,
            ),
        )
        self.callback_id = build_attempt_callback_id(self.attempt)
        self.callback_nonce = TEST_CALLBACK_NONCE

        self.callback_url = "/api/v1/validation-callbacks/"

    def _make_mock_envelope(self):
        """Create a mock output envelope for testing.

        Uses proper enum values and serializable types to match what
        the callback handler expects and stores.
        """
        mock_envelope = MagicMock()
        # Use actual enum - the handler uses this as a dict key
        mock_envelope.status = ValidationStatus.SUCCESS
        # Validator info - must have string values (stored in JSONField)
        mock_envelope.validator = MagicMock()
        mock_envelope.validator.id = str(self.validator.id)
        mock_envelope.validator.type = ValidationType.ENERGYPLUS
        mock_envelope.validator.version = "1.0.0"
        # Run/org/workflow identifiers
        mock_envelope.run_id = str(self.run.id)
        mock_envelope.step_run_id = str(self.step_run.pk)
        mock_envelope.execution_attempt_id = str(self.attempt.pk)
        mock_envelope.attempt_contract_version = "validibot.attempt.v2"
        mock_envelope.input_envelope_sha256 = self.attempt.input_envelope_sha256
        mock_envelope.output_uri = self.attempt.output_envelope_uri
        mock_envelope.org = MagicMock()
        mock_envelope.org.id = str(self.org.id)
        mock_envelope.workflow = MagicMock()
        mock_envelope.workflow.step_id = str(self.workflow_step.id)
        # Timing info
        mock_envelope.timing = MagicMock()
        mock_envelope.timing.finished_at = None
        # Messages and outputs - empty for success case
        mock_envelope.messages = []
        mock_envelope.outputs = MagicMock()
        mock_envelope.outputs.output_values = {}
        # model_dump returns the JSON-serializable callback receipt payload.
        mock_envelope.model_dump.return_value = {
            "status": "success",
            "run_id": str(self.run.id),
        }
        return mock_envelope

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_first_callback_processed_successfully(self, mock_download):
        """Test that the first callback with a callback_id is processed."""
        mock_download.return_value = self._make_mock_envelope()

        callback_id = self.callback_id
        payload = {
            "run_id": str(self.run.id),
            "callback_id": callback_id,
            "callback_nonce": self.callback_nonce,
            "status": "success",
            "result_uri": "gs://bucket/output.json",
        }

        response = self.client.post(
            self.callback_url,
            data=payload,
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["message"], "Callback processed successfully")

        # Verify receipt was created and updated to COMPLETED status
        receipt = CallbackReceipt.objects.filter(callback_id=callback_id).first()
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.validation_run_id, self.run.id)
        # Receipt should be updated from PROCESSING to COMPLETED
        self.assertEqual(receipt.status, CallbackReceiptStatus.COMPLETED)

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_permanent_error_marks_receipt_rejected(self, mock_download):
        """A permanent (4xx) callback error must move the receipt to the
        terminal REJECTED state.

        Why this matters: before a terminal state existed, a callback that
        could never succeed (here, an output envelope whose ``validator.id``
        doesn't match the run's validator) left the receipt in PROCESSING — so
        every Cloud Tasks redelivery re-ran the doomed processing. Marking it
        REJECTED lets the idempotency guard short-circuit retries and records
        the outcome honestly. We assert the response is the 4xx error AND the
        receipt ends REJECTED.
        """
        # The envelope downloads fine (mocked) but its validator.id mismatches
        # the run's validator → a permanent 400 in _download_and_validate_envelope.
        mock_envelope = self._make_mock_envelope()
        mock_envelope.validator.id = str(uuid.uuid4())
        mock_download.return_value = mock_envelope

        callback_id = self.callback_id
        payload = {
            "run_id": str(self.run.id),
            "callback_id": callback_id,
            "callback_nonce": self.callback_nonce,
            "status": "success",
            "result_uri": "gs://bucket/output.json",
        }

        response = self.client.post(self.callback_url, data=payload, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        receipt = CallbackReceipt.objects.filter(callback_id=callback_id).first()
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.status, CallbackReceiptStatus.REJECTED)

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_rejected_callback_retry_short_circuits(self, mock_download):
        """A redelivery of a permanently REJECTED callback must NOT reprocess.

        Once a callback is REJECTED (terminal), a Cloud Tasks redelivery hits
        the receipt before terminal-attempt handling and returns a cached 200
        without re-running processing. We prove that by asserting
        download_envelope is not called a second time.
        """
        mock_envelope = self._make_mock_envelope()
        mock_envelope.validator.id = str(uuid.uuid4())
        mock_download.return_value = mock_envelope

        callback_id = self.callback_id
        payload = {
            "run_id": str(self.run.id),
            "callback_id": callback_id,
            "callback_nonce": self.callback_nonce,
            "status": "success",
            "result_uri": "gs://bucket/output.json",
        }

        # First delivery → permanent rejection.
        first = self.client.post(self.callback_url, data=payload, format="json")
        self.assertEqual(first.status_code, status.HTTP_400_BAD_REQUEST)
        calls_after_first = mock_download.call_count

        # Redelivery → short-circuit to a cached 200, no reprocessing.
        second = self.client.post(self.callback_url, data=payload, format="json")
        self.assertEqual(second.status_code, status.HTTP_200_OK)
        self.assertTrue(second.data.get("idempotent_replayed"))
        self.assertEqual(second.data["message"], "Callback already rejected")
        # download_envelope must NOT have been called again for the redelivery.
        self.assertEqual(mock_download.call_count, calls_after_first)

        receipt = CallbackReceipt.objects.filter(callback_id=callback_id).first()
        self.assertEqual(receipt.status, CallbackReceiptStatus.REJECTED)

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_duplicate_callback_returns_early(self, mock_download):
        """Test that duplicate callbacks with same callback_id are not reprocessed."""
        mock_download.return_value = self._make_mock_envelope()

        callback_id = self.callback_id
        payload = {
            "run_id": str(self.run.id),
            "callback_id": callback_id,
            "callback_nonce": self.callback_nonce,
            "status": "success",
            "result_uri": "gs://bucket/output.json",
        }

        # First callback - should be processed
        response1 = self.client.post(
            self.callback_url,
            data=payload,
            format="json",
        )
        self.assertEqual(response1.status_code, status.HTTP_200_OK)

        # Reset mock call count
        mock_download.reset_mock()

        # Second callback with same callback_id - should return early
        response2 = self.client.post(
            self.callback_url,
            data=payload,
            format="json",
        )

        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        self.assertTrue(response2.data.get("idempotent_replayed"))
        self.assertEqual(response2.data["message"], "Callback already processed")
        self.assertIn("original_received_at", response2.data)

        # Verify download_envelope was NOT called for the duplicate
        mock_download.assert_not_called()

        # Verify only one receipt exists
        receipt_count = CallbackReceipt.objects.filter(
            callback_id=callback_id,
        ).count()
        self.assertEqual(receipt_count, 1)

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_duplicate_requires_the_original_attempt_nonce(self, mock_download):
        """A known callback ID must not expose a cached success without proof.

        Receipt replay is an externally reachable path, so authentication must
        happen before the idempotency shortcut as well as before storage reads.
        """
        mock_download.return_value = self._make_mock_envelope()
        payload = {
            "run_id": str(self.run.id),
            "callback_id": self.callback_id,
            "callback_nonce": self.callback_nonce,
            "status": "success",
            "result_uri": "gs://bucket/output.json",
        }
        first = self.client.post(self.callback_url, data=payload, format="json")
        self.assertEqual(first.status_code, status.HTTP_200_OK)

        mock_download.reset_mock()
        payload["callback_nonce"] = WRONG_CALLBACK_NONCE
        replay = self.client.post(self.callback_url, data=payload, format="json")

        self.assertEqual(replay.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(replay.data["error"], "Invalid callback credentials")
        mock_download.assert_not_called()
        self.assertEqual(
            CallbackReceipt.objects.filter(callback_id=self.callback_id).count(),
            1,
        )

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_callback_without_attempt_id_is_rejected(self, mock_download):
        """The shared callback schema rejects missing attempt identity early."""
        mock_download.return_value = self._make_mock_envelope()

        payload = {
            "run_id": str(self.run.id),
            # No callback_id
            "callback_nonce": self.callback_nonce,
            "status": "success",
            "result_uri": "gs://bucket/output.json",
        }

        response = self.client.post(
            self.callback_url,
            data=payload,
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(response.data["error"], "Invalid callback payload")

        # No receipt created when callback_id is missing
        self.assertEqual(CallbackReceipt.objects.count(), 0)

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_different_attempt_callbacks_are_processed_separately(self, mock_download):
        """Callbacks for distinct attempts are processed independently.

        Each provider launch has its own durable callback identity and receipt.
        """
        mock_download.return_value = self._make_mock_envelope()

        callback_id_1 = self.callback_id
        self.attempt.output_envelope_uri = "gs://bucket/output1.json"
        self.attempt.save(update_fields=["output_envelope_uri"])
        mock_download.return_value.output_uri = self.attempt.output_envelope_uri

        # First callback
        payload1 = {
            "run_id": str(self.run.id),
            "callback_id": callback_id_1,
            "callback_nonce": self.callback_nonce,
            "status": "success",
            "result_uri": "gs://bucket/output1.json",
        }
        response1 = self.client.post(self.callback_url, data=payload1, format="json")
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        self.assertNotIn("idempotent_replayed", response1.data)

        # Create a second run with its own step run for the second callback
        run2 = ValidationRunFactory(
            org=self.org,
            user=self.user,
            status=ValidationRunStatus.RUNNING,
        )
        workflow_step2 = WorkflowStepFactory(
            workflow=run2.workflow,
            validator=self.validator,
        )
        step_run2 = ValidationStepRunFactory(
            validation_run=run2,
            workflow_step=workflow_step2,
            status=StepStatus.RUNNING,
        )
        attempt2 = ExecutionAttemptFactory(
            step_run=step_run2,
            state="RUNNING",
            callback_nonce_hash=build_callback_nonce_verifier(
                TEST_CALLBACK_NONCE,
            ),
            output_envelope_uri="gs://bucket/output2.json",
        )

        # Update mock envelope for run2
        mock_envelope2 = self._make_mock_envelope()
        mock_envelope2.run_id = str(run2.id)
        mock_envelope2.step_run_id = str(step_run2.pk)
        mock_envelope2.execution_attempt_id = str(attempt2.pk)
        mock_envelope2.input_envelope_sha256 = attempt2.input_envelope_sha256
        mock_envelope2.output_uri = attempt2.output_envelope_uri
        mock_envelope2.org.id = str(self.org.id)
        mock_envelope2.workflow.step_id = str(workflow_step2.id)
        mock_download.return_value = mock_envelope2

        callback_id_2 = build_attempt_callback_id(attempt2)

        # Second callback with different callback_id
        payload2 = {
            "run_id": str(run2.id),
            "callback_id": callback_id_2,
            "callback_nonce": TEST_CALLBACK_NONCE,
            "status": "success",
            "result_uri": "gs://bucket/output2.json",
        }
        response2 = self.client.post(self.callback_url, data=payload2, format="json")
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        self.assertNotIn("idempotent_replayed", response2.data)

        # Both receipts should exist
        self.assertEqual(CallbackReceipt.objects.count(), 2)
        self.assertTrue(
            CallbackReceipt.objects.filter(callback_id=callback_id_1).exists()
        )
        self.assertTrue(
            CallbackReceipt.objects.filter(callback_id=callback_id_2).exists()
        )

    @override_settings(APP_IS_WORKER=False)
    def test_callback_rejected_on_non_worker(self):
        """Test that callbacks are rejected on non-worker instances."""
        payload = {
            "run_id": str(self.run.id),
            "callback_id": str(uuid.uuid4()),
            "callback_nonce": self.callback_nonce,
            "status": "success",
            "result_uri": "gs://bucket/output.json",
        }

        response = self.client.post(
            self.callback_url,
            data=payload,
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_retry_when_processor_fails_receipt_stays_processing(self, mock_download):
        """
        Test that when processing fails, the receipt stays PROCESSING and retry works.

        Scenario:
        1. First callback arrives, receipt created with PROCESSING status
        2. Processor throws an exception mid-processing
        3. Receipt stays in PROCESSING state (not updated to terminal)
        4. Second callback arrives with same callback_id
        5. System detects PROCESSING status and allows retry
        6. Second attempt succeeds
        """
        from validibot.core.models import CallbackReceiptStatus

        # First call: simulate processor failure by throwing exception
        mock_download.side_effect = Exception("Simulated GCS download failure")

        callback_id = self.callback_id
        payload = {
            "run_id": str(self.run.id),
            "callback_id": callback_id,
            "callback_nonce": self.callback_nonce,
            "status": "success",
            "result_uri": "gs://bucket/output.json",
        }

        # First attempt - should fail with 500
        response1 = self.client.post(
            self.callback_url,
            data=payload,
            format="json",
        )
        self.assertEqual(response1.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)

        # Verify receipt exists and is in PROCESSING state
        receipt = CallbackReceipt.objects.get(callback_id=callback_id)
        self.assertEqual(receipt.status, CallbackReceiptStatus.PROCESSING)

        # Second call: processor succeeds this time
        mock_download.side_effect = None
        mock_download.return_value = self._make_mock_envelope()

        # Second attempt - should succeed (retry detected)
        response2 = self.client.post(
            self.callback_url,
            data=payload,
            format="json",
        )

        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        self.assertEqual(response2.data["message"], "Callback processed successfully")
        # NOT marked as idempotent_replayed because it was a retry, not a duplicate
        self.assertNotIn("idempotent_replayed", response2.data)

        # Verify receipt is now updated to COMPLETED
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, CallbackReceiptStatus.COMPLETED)

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_no_retry_when_receipt_has_terminal_status(self, mock_download):
        """
        Test that callbacks with terminal receipt status are NOT retried.

        Once a callback has been successfully processed (terminal status),
        subsequent callbacks with the same callback_id return the cached
        receipt response rather than reprocessing output.
        """
        mock_download.return_value = self._make_mock_envelope()

        callback_id = self.callback_id
        payload = {
            "run_id": str(self.run.id),
            "callback_id": callback_id,
            "callback_nonce": self.callback_nonce,
            "status": "success",
            "result_uri": "gs://bucket/output.json",
        }

        # First attempt - should succeed
        response1 = self.client.post(
            self.callback_url,
            data=payload,
            format="json",
        )
        self.assertEqual(response1.status_code, status.HTTP_200_OK)
        self.assertEqual(response1.data["message"], "Callback processed successfully")

        # Verify receipt has terminal status (COMPLETED, not "success")
        receipt = CallbackReceipt.objects.get(callback_id=callback_id)
        self.assertEqual(receipt.status, CallbackReceiptStatus.COMPLETED)

        # Reset mock to track if it gets called
        mock_download.reset_mock()

        # Second attempt returns the cached receipt without reprocessing.
        response2 = self.client.post(
            self.callback_url,
            data=payload,
            format="json",
        )

        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        self.assertTrue(response2.data.get("idempotent_replayed"))
        self.assertEqual(response2.data["message"], "Callback already processed")

        # Verify download_envelope was NOT called (no reprocessing)
        mock_download.assert_not_called()


class CallbackReceiptModelTestCase(TestCase):
    """Test the CallbackReceipt model."""

    def setUp(self):
        self.org = OrganizationFactory()
        self.user = UserFactory(orgs=[self.org])
        self.run = ValidationRunFactory(
            org=self.org,
            user=self.user,
            status=ValidationRunStatus.RUNNING,
        )
        self.attempt = ExecutionAttemptFactory(
            step_run__validation_run=self.run,
            state="RUNNING",
        )

    def test_callback_id_unique_constraint(self):
        """Test that callback_id must be unique."""
        import pytest
        from django.db import IntegrityError

        callback_id = str(uuid.uuid4())

        # Create first receipt
        CallbackReceipt.objects.create(
            callback_id=callback_id,
            validation_run=self.run,
            execution_attempt=self.attempt,
            status=CallbackReceiptStatus.COMPLETED,
        )

        # Attempt to create duplicate should raise IntegrityError
        with pytest.raises(IntegrityError):
            CallbackReceipt.objects.create(
                callback_id=callback_id,
                validation_run=self.run,
                execution_attempt=self.attempt,
                status=CallbackReceiptStatus.COMPLETED,
            )

    def test_str_representation(self):
        """Test the string representation of CallbackReceipt."""
        callback_id = "12345678-1234-1234-1234-123456789012"
        receipt = CallbackReceipt.objects.create(
            callback_id=callback_id,
            validation_run=self.run,
            execution_attempt=self.attempt,
            status=CallbackReceiptStatus.COMPLETED,
        )

        expected = f"CallbackReceipt(12345678... for run {self.run.id})"
        self.assertEqual(str(receipt), expected)

    def test_receipt_stores_metadata(self):
        """Test that receipt stores status and result_uri."""
        callback_id = str(uuid.uuid4())
        result_uri = "gs://bucket/runs/abc/output.json"

        receipt = CallbackReceipt.objects.create(
            callback_id=callback_id,
            validation_run=self.run,
            execution_attempt=self.attempt,
            status=CallbackReceiptStatus.COMPLETED,
            result_uri=result_uri,
        )

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, CallbackReceiptStatus.COMPLETED)
        self.assertEqual(receipt.result_uri, result_uri)
        self.assertIsNotNone(receipt.received_at)
