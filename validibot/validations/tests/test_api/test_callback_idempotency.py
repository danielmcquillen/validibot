"""
Tests for Cloud Run Job callback idempotency.

These tests verify that duplicate callback deliveries from Cloud Run are
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

from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import CallbackReceipt
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


class CallbackIdempotencyTestCase(TestCase):
    """Test callback idempotency for Cloud Run Job callbacks."""

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

        self.callback_url = "/api/v1/validation-callbacks/"

    def _make_mock_envelope(self):
        """Create a mock output envelope for testing."""
        mock_envelope = MagicMock()
        mock_envelope.status = MagicMock()
        mock_envelope.status.value = "success"
        mock_envelope.validator = MagicMock()
        mock_envelope.validator.id = str(self.validator.id)
        mock_envelope.run_id = str(self.run.id)
        mock_envelope.org = MagicMock()
        mock_envelope.org.id = str(self.org.id)
        mock_envelope.workflow = MagicMock()
        mock_envelope.workflow.step_id = str(self.workflow_step.id)
        mock_envelope.timing = MagicMock()
        mock_envelope.timing.finished_at = None
        mock_envelope.messages = []
        mock_envelope.outputs = MagicMock()
        mock_envelope.outputs.output_values = {}
        mock_envelope.model_dump = MagicMock(return_value={})
        return mock_envelope

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.api.callbacks.download_envelope")
    def test_first_callback_processed_successfully(self, mock_download):
        """Test that the first callback with a callback_id is processed."""
        mock_download.return_value = self._make_mock_envelope()

        callback_id = str(uuid.uuid4())
        payload = {
            "run_id": str(self.run.id),
            "callback_id": callback_id,
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

        # Verify receipt was created and updated to final status
        receipt = CallbackReceipt.objects.filter(callback_id=callback_id).first()
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt.validation_run_id, self.run.id)
        # Receipt should be updated from "processing" to final status
        self.assertEqual(receipt.status, "success")

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.api.callbacks.download_envelope")
    def test_duplicate_callback_returns_early(self, mock_download):
        """Test that duplicate callbacks with same callback_id are not reprocessed."""
        mock_download.return_value = self._make_mock_envelope()

        callback_id = str(uuid.uuid4())
        payload = {
            "run_id": str(self.run.id),
            "callback_id": callback_id,
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
    @patch("validibot.validations.api.callbacks.download_envelope")
    def test_callback_without_id_still_processed(self, mock_download):
        """Test that callbacks without callback_id are processed (no idempotency)."""
        mock_download.return_value = self._make_mock_envelope()

        payload = {
            "run_id": str(self.run.id),
            # No callback_id
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

        # No receipt created when callback_id is missing
        self.assertEqual(CallbackReceipt.objects.count(), 0)

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    @patch("validibot.validations.api.callbacks.download_envelope")
    def test_different_callback_ids_processed_separately(self, mock_download):
        """Test that different callback_ids are processed independently."""
        mock_download.return_value = self._make_mock_envelope()

        callback_id_1 = str(uuid.uuid4())
        callback_id_2 = str(uuid.uuid4())

        # First callback
        payload1 = {
            "run_id": str(self.run.id),
            "callback_id": callback_id_1,
            "status": "success",
            "result_uri": "gs://bucket/output1.json",
        }
        response1 = self.client.post(self.callback_url, data=payload1, format="json")
        self.assertEqual(response1.status_code, status.HTTP_200_OK)

        # Create new step run for second callback
        self.step_run2 = ValidationStepRunFactory(
            validation_run=self.run,
            workflow_step=self.workflow_step,
            status=StepStatus.RUNNING,
        )

        # Second callback with different callback_id
        payload2 = {
            "run_id": str(self.run.id),
            "callback_id": callback_id_2,
            "status": "success",
            "result_uri": "gs://bucket/output2.json",
        }
        response2 = self.client.post(self.callback_url, data=payload2, format="json")
        self.assertEqual(response2.status_code, status.HTTP_200_OK)
        self.assertNotIn("idempotent_replayed", response2.data)

        # Both receipts should exist
        self.assertEqual(CallbackReceipt.objects.count(), 2)

    @override_settings(APP_IS_WORKER=False)
    def test_callback_rejected_on_non_worker(self):
        """Test that callbacks are rejected on non-worker instances."""
        payload = {
            "run_id": str(self.run.id),
            "callback_id": str(uuid.uuid4()),
            "status": "success",
            "result_uri": "gs://bucket/output.json",
        }

        response = self.client.post(
            self.callback_url,
            data=payload,
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


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

    def test_callback_id_unique_constraint(self):
        """Test that callback_id must be unique."""
        import pytest
        from django.db import IntegrityError

        callback_id = str(uuid.uuid4())

        # Create first receipt
        CallbackReceipt.objects.create(
            callback_id=callback_id,
            validation_run=self.run,
            status="success",
        )

        # Attempt to create duplicate should raise IntegrityError
        with pytest.raises(IntegrityError):
            CallbackReceipt.objects.create(
                callback_id=callback_id,
                validation_run=self.run,
                status="success",
            )

    def test_str_representation(self):
        """Test the string representation of CallbackReceipt."""
        callback_id = "12345678-1234-1234-1234-123456789012"
        receipt = CallbackReceipt.objects.create(
            callback_id=callback_id,
            validation_run=self.run,
            status="success",
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
            status="failed_validation",
            result_uri=result_uri,
        )

        receipt.refresh_from_db()
        self.assertEqual(receipt.status, "failed_validation")
        self.assertEqual(receipt.result_uri, result_uri)
        self.assertIsNotNone(receipt.received_at)
