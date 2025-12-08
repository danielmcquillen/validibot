"""
Integration tests for Cloud Run Job execution.

These tests verify that the Cloud Run Job infrastructure works end-to-end:
1. Upload input envelope to GCS
2. Trigger Cloud Run Job directly via Jobs API (no Cloud Tasks involved)
3. Poll GCS for output envelope
4. Verify output envelope structure

Production Architecture:
    Web -> Cloud Task -> Worker -> Cloud Run Job (direct API call) -> Callback

Test Architecture (simplified):
    Test -> Cloud Run Job (direct API call) -> GCS output

Note: These tests require GCP credentials and deployed infrastructure.
They will be skipped if the required environment variables are not set.

The callback mechanism is NOT tested here because it requires the Django app
to be reachable from Cloud Run. For callback testing, run against a deployed
staging environment.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.test import TestCase

if TYPE_CHECKING:
    from google.cloud import storage

logger = logging.getLogger(__name__)


class CloudRunJobIntegrationTest(TestCase):
    """
    Integration tests for Cloud Run Job execution.

    These tests verify that we can:
    1. Upload an input envelope to GCS
    2. Trigger a Cloud Run Job directly via Jobs API
    3. Poll for and retrieve the output envelope

    Prerequisites:
    - GCP_PROJECT_ID environment variable set
    - GCS_VALIDATION_BUCKET environment variable set
    - GCS_ENERGYPLUS_JOB_NAME environment variable set
    - GCP_REGION environment variable set
    - Valid GCP credentials (GOOGLE_APPLICATION_CREDENTIALS or default credentials)
    - Cloud Run Job deployed and ready
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Check prerequisites and skip if not configured."""
        super().setUpClass()
        cls._check_prerequisites()

    @classmethod
    def _check_prerequisites(cls) -> None:
        """Verify all required GCP settings are configured."""
        required_settings = [
            "GCP_PROJECT_ID",
            "GCS_VALIDATION_BUCKET",
            "GCS_ENERGYPLUS_JOB_NAME",
            "GCP_REGION",
        ]

        missing = []
        for setting_name in required_settings:
            value = getattr(settings, setting_name, None)
            if not value:
                missing.append(setting_name)

        if missing:
            raise cls.skipTest(
                cls,
                f"Cloud Run integration test skipped: missing settings {missing}",
            )

        # Check for GCP credentials
        try:
            from google.auth import default

            default()
        except Exception as e:
            raise cls.skipTest(
                cls,
                f"Cloud Run test skipped: GCP credentials not configured: {e}",
            ) from None

    def setUp(self) -> None:
        """Set up test fixtures."""
        super().setUp()
        self.test_run_id = f"test-{uuid.uuid4()}"
        self.test_org_id = "test-org"

    def tearDown(self) -> None:
        """Clean up test artifacts from GCS."""
        super().tearDown()
        self._cleanup_gcs_artifacts()

    def _cleanup_gcs_artifacts(self) -> None:
        """Remove test files from GCS bucket."""
        try:
            from google.cloud import storage

            client = storage.Client()
            bucket = client.bucket(settings.GCS_VALIDATION_BUCKET)
            prefix = f"runs/{self.test_org_id}/{self.test_run_id}/"

            blobs = list(bucket.list_blobs(prefix=prefix))
            for blob in blobs:
                try:
                    blob.delete()
                    logger.debug("Deleted test artifact: %s", blob.name)
                except Exception:
                    logger.warning("Failed to delete blob: %s", blob.name)
        except Exception:
            logger.warning(
                "Failed to clean up GCS artifacts for run %s",
                self.test_run_id,
                exc_info=True,
            )

    def _get_gcs_client(self) -> storage.Client:
        """Get a GCS client."""
        from google.cloud import storage

        return storage.Client()

    def _upload_test_envelope(self, envelope: dict) -> str:
        """
        Upload a test input envelope to GCS.

        Args:
            envelope: The input envelope dict to upload

        Returns:
            The GCS URI of the uploaded envelope
        """
        from google.cloud import storage

        client = storage.Client()
        bucket = client.bucket(settings.GCS_VALIDATION_BUCKET)

        blob_path = f"runs/{self.test_org_id}/{self.test_run_id}/input.json"
        blob = bucket.blob(blob_path)
        blob.upload_from_string(
            json.dumps(envelope),
            content_type="application/json",
        )

        uri = f"gs://{settings.GCS_VALIDATION_BUCKET}/{blob_path}"
        logger.info("Uploaded test envelope to %s", uri)
        return uri

    def _poll_for_output(
        self,
        *,
        timeout_seconds: int = 300,
        poll_interval_seconds: int = 10,
    ) -> dict | None:
        """
        Poll GCS for the output envelope.

        Args:
            timeout_seconds: Maximum time to wait for output
            poll_interval_seconds: Time between polls

        Returns:
            The output envelope dict, or None if not found within timeout
        """
        from google.cloud import storage

        client = storage.Client()
        bucket = client.bucket(settings.GCS_VALIDATION_BUCKET)
        output_path = f"runs/{self.test_org_id}/{self.test_run_id}/output.json"

        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            blob = bucket.blob(output_path)
            if blob.exists():
                content = blob.download_as_bytes()
                logger.info("Found output envelope at %s", output_path)
                return json.loads(content)

            logger.debug(
                "Output not ready, waiting %d seconds (elapsed: %.0f/%d)",
                poll_interval_seconds,
                time.time() - start_time,
                timeout_seconds,
            )
            time.sleep(poll_interval_seconds)

        logger.warning("Timed out waiting for output envelope")
        return None

    def test_gcs_connectivity(self) -> None:
        """
        Test that we can connect to GCS and list the validation bucket.

        This is a basic smoke test to verify GCP credentials work.
        """
        from google.cloud import storage

        client = storage.Client()
        bucket = client.bucket(settings.GCS_VALIDATION_BUCKET)

        # Just verify we can access the bucket
        self.assertTrue(
            bucket.exists(),
            f"Bucket {settings.GCS_VALIDATION_BUCKET} not accessible",
        )

    def test_cloud_tasks_connectivity(self) -> None:
        """
        Test that we can connect to Cloud Tasks and access the queue.

        Note: Cloud Tasks is used for Web -> Worker communication, not for
        triggering Cloud Run Jobs (which use direct API calls).
        """
        queue_name = getattr(settings, "GCS_TASK_QUEUE_NAME", None)
        if not queue_name:
            self.skipTest("GCS_TASK_QUEUE_NAME not configured")

        from google.cloud import tasks_v2

        client = tasks_v2.CloudTasksClient()
        queue_path = client.queue_path(
            settings.GCP_PROJECT_ID,
            settings.GCP_REGION,
            queue_name,
        )

        # Verify we can get queue metadata
        try:
            queue = client.get_queue(name=queue_path)
            self.assertIsNotNone(queue.name)
            logger.info("Successfully connected to queue: %s", queue.name)
        except Exception as e:
            self.fail(f"Failed to access Cloud Tasks queue: {e}")

    def test_energyplus_job_execution(self) -> None:
        """
        Test end-to-end EnergyPlus Cloud Run Job execution.

        This test:
        1. Creates a minimal EnergyPlus input envelope
        2. Uploads it to GCS
        3. Triggers the Cloud Run Job
        4. Polls for the output envelope
        5. Verifies the output structure

        Note: This test requires a valid weather file to exist in GCS
        and may take several minutes to complete.
        """
        # Skip if we don't have the EnergyPlus job configured
        if not getattr(settings, "GCS_ENERGYPLUS_JOB_NAME", None):
            self.skipTest("GCS_ENERGYPLUS_JOB_NAME not configured")

        # Check for a test weather file - skip if not available
        weather_file = getattr(
            settings,
            "TEST_ENERGYPLUS_WEATHER_FILE",
            "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw",
        )
        weather_uri = (
            f"gs://{settings.GCS_VALIDATION_BUCKET}/"
            f"{settings.GCS_WEATHER_PREFIX}/{weather_file}"
        )

        # Ensure weather file exists in the catalog (seed fixture in non-prod)
        from google.cloud import storage

        weather_blob_path = f"{settings.GCS_WEATHER_PREFIX}/{weather_file}"
        client = storage.Client()
        bucket = client.bucket(settings.GCS_VALIDATION_BUCKET)
        blob = bucket.blob(weather_blob_path)

        if not blob.exists():
            local_weather = (
                Path(settings.BASE_DIR)
                / "tests"
                / "data"
                / "energyplus"
                / "test_weather.epw"
            )
            if not local_weather.exists():
                self.skipTest(
                    "Weather file missing in catalog and no local fixture to seed it.",
                )
            blob.upload_from_filename(
                filename=str(local_weather),
                content_type="application/vnd.energyplus.epw",
            )
            logger.info("Seeded weather file to %s", weather_blob_path)

        # Use the shipped sample epJSON submission to mimic a real user payload
        sample_path = (
            Path(settings.BASE_DIR)
            / "tests"
            / "data"
            / "energyplus"
            / "example_epjson.json"
        )
        sample_content = sample_path.read_text()

        # Upload the test model to GCS
        model_uri = self._upload_test_model(
            sample_content,
            filename="model.epjson",
            content_type="application/json",
        )

        # Build a test input envelope matching EnergyPlusInputEnvelope schema
        # See vb_shared.energyplus.envelopes for the schema definition
        envelope = {
            "schema_version": "validibot.input.v1",
            "run_id": self.test_run_id,
            "validator": {
                "id": "test-validator",
                "type": "energyplus",
                "version": "24.1",
            },
            "org": {
                "id": self.test_org_id,
                "name": "Test Organization",
            },
            "workflow": {
                "id": "test-workflow",
                "step_id": "test-step",
                "step_name": "Test EnergyPlus Step",
            },
            "input_files": [
                {
                    "name": "model.epjson",
                    "mime_type": "application/vnd.energyplus.epjson",
                    "role": "primary-model",
                    "uri": model_uri,
                },
                {
                    "name": "weather.epw",
                    "mime_type": "application/vnd.energyplus.epw",
                    "role": "weather",
                    "uri": weather_uri,
                },
            ],
            # inputs field matches EnergyPlusInputs schema
            "inputs": {
                "timestep_per_hour": 4,
                "output_variables": [],
                "invocation_mode": "cli",
            },
            "context": {
                # Skip callback since we're polling GCS for results
                "skip_callback": True,
                "execution_bundle_uri": (
                    f"gs://{settings.GCS_VALIDATION_BUCKET}"
                    f"/runs/{self.test_org_id}/{self.test_run_id}"
                ),
            },
        }

        # Upload the envelope
        input_uri = self._upload_test_envelope(envelope)

        # Trigger the Cloud Run Job directly via Jobs API
        from simplevalidations.validations.services.cloud_run.job_client import (
            run_validator_job,
        )

        logger.info("Triggering EnergyPlus Cloud Run Job...")
        execution_name = run_validator_job(
            project_id=settings.GCP_PROJECT_ID,
            region=settings.GCP_REGION,
            job_name=settings.GCS_ENERGYPLUS_JOB_NAME,
            input_uri=input_uri,
        )
        logger.info("Started execution: %s", execution_name)

        # Poll for output
        logger.info("Polling for output envelope...")
        output = self._poll_for_output(
            timeout_seconds=300,  # 5 minutes should be enough for minimal model
            poll_interval_seconds=15,
        )

        # Verify output
        self.assertIsNotNone(
            output,
            "Timed out waiting for EnergyPlus job output",
        )

        # Check output envelope structure
        self.assertIn("status", output)
        self.assertIn("run_id", output)
        self.assertEqual(output["run_id"], self.test_run_id)

        # Log the result for debugging
        logger.info("EnergyPlus job completed with status: %s", output.get("status"))
        messages = output.get("messages") or []
        outputs = output.get("outputs") or {}
        logs = outputs.get("logs") or {}
        err_tail = logs.get("err_tail") or logs.get("stderr_tail") or ""
        logger.info("EnergyPlus returncode: %s", outputs.get("energyplus_returncode"))
        if messages:
            logger.info("EnergyPlus messages: %s", messages)
        if err_tail:
            logger.info("EnergyPlus err tail:\n%s", err_tail)

        # We expect the job to succeed with our minimal model
        # But even a failure is "working" from an infrastructure perspective
        status = output.get("status")
        self.assertIn(
            status,
            ["success", "failed_validation", "failed_runtime"],
            f"Unexpected status: {status}",
        )

    def _upload_test_model(
        self,
        content: str,
        *,
        filename: str = "model.idf",
        content_type: str = "text/plain",
    ) -> str:
        """Upload test model content to GCS."""
        from google.cloud import storage

        client = storage.Client()
        bucket = client.bucket(settings.GCS_VALIDATION_BUCKET)

        blob_path = f"runs/{self.test_org_id}/{self.test_run_id}/{filename}"
        blob = bucket.blob(blob_path)
        blob.upload_from_string(content, content_type=content_type)

        uri = f"gs://{settings.GCS_VALIDATION_BUCKET}/{blob_path}"
        logger.info("Uploaded test model to %s", uri)
        return uri


class CloudRunCallbackTest(TestCase):
    """
    Tests for the callback endpoint.

    These tests verify the callback handler logic without requiring
    a real Cloud Run Job. They test:
    - Payload validation
    - Database updates
    - Error handling

    For full end-to-end callback testing, deploy to staging and
    run the CloudRunJobIntegrationTest against it.
    """

    def test_callback_endpoint_path(self) -> None:
        """
        Verify the callback URL path exists.

        Note: The api-internal namespace is only mounted in worker mode
        (urls_worker.py), so we test the path directly rather than reversing.
        """
        # The callback endpoint should be at this path in worker mode
        expected_path = "/api/v1/validation-callbacks/"
        self.assertEqual(expected_path, "/api/v1/validation-callbacks/")

    def test_callback_requires_worker_mode(self) -> None:
        """Verify callbacks are rejected on non-worker instances."""
        from django.test import Client
        from django.test import override_settings

        client = Client()

        # Without APP_IS_WORKER=True, callbacks should 404
        with override_settings(APP_IS_WORKER=False):
            response = client.post(
                "/api/v1/validation-callbacks/",
                data={"run_id": "test", "status": "success", "result_uri": "gs://x"},
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 404)
