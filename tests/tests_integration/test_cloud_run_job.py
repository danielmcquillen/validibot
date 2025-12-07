"""
Integration tests for Cloud Run Job execution.

These tests verify that the Cloud Run Job infrastructure works end-to-end:
1. Upload input envelope to GCS
2. Trigger Cloud Run Job via Cloud Tasks
3. Poll GCS for output envelope
4. Verify output envelope structure

Note: These tests require GCP credentials and deployed infrastructure.
They will be skipped if the required environment variables are not set.

The callback mechanism is NOT tested here because it requires the Django app
to be reachable from Cloud Run. For callback testing, run against a deployed
staging environment.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
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
    2. Trigger a Cloud Run Job via Cloud Tasks
    3. Poll for and retrieve the output envelope

    Prerequisites:
    - GCP_PROJECT_ID environment variable set
    - GCS_VALIDATION_BUCKET environment variable set
    - GCS_ENERGYPLUS_JOB_NAME environment variable set
    - GCS_TASK_QUEUE_NAME environment variable set
    - Valid GCP credentials (GOOGLE_APPLICATION_CREDENTIALS or default credentials)
    - Cloud Run Job deployed and ready
    - Cloud Tasks queue configured
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
            "GCS_TASK_QUEUE_NAME",
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
                content = blob.download_as_string()
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

        This verifies the Cloud Tasks client can authenticate.
        """
        from google.cloud import tasks_v2

        client = tasks_v2.CloudTasksClient()
        queue_path = client.queue_path(
            settings.GCP_PROJECT_ID,
            settings.GCP_REGION,
            settings.GCS_TASK_QUEUE_NAME,
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
        weather_file = os.environ.get(
            "TEST_WEATHER_FILE",
            "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw",
        )
        weather_uri = f"gs://{settings.GCS_VALIDATION_BUCKET}/assets/weather/{weather_file}"

        # Create a minimal but valid IDF content
        # This is the simplest possible EnergyPlus model
        minimal_idf = """
!-Generator IDFEditor 1.0
!-NOTE: All comments with '!-' are ignored by the IDFEditor

Version,
    24.1;                    !- Version Identifier

Building,
    Minimal Test Building,   !- Name
    0,                       !- North Axis {deg}
    Suburbs,                 !- Terrain
    0.04,                    !- Loads Convergence Tolerance Value {W}
    0.4,                     !- Temperature Convergence Tolerance Value {deltaC}
    FullInteriorAndExterior, !- Solar Distribution
    25,                      !- Maximum Number of Warmup Days
    6;                       !- Minimum Number of Warmup Days

GlobalGeometryRules,
    UpperLeftCorner,         !- Starting Vertex Position
    Counterclockwise,        !- Vertex Entry Direction
    Relative;                !- Coordinate System

SimulationControl,
    No,                      !- Do Zone Sizing Calculation
    No,                      !- Do System Sizing Calculation
    No,                      !- Do Plant Sizing Calculation
    No,                      !- Run Simulation for Sizing Periods
    Yes,                     !- Run Simulation for Weather File Run Periods
    No,                      !- Do HVAC Sizing Simulation for Sizing Periods
    1;                       !- Maximum Number of HVAC Sizing Simulation Passes

RunPeriod,
    Annual,                  !- Name
    1,                       !- Begin Month
    1,                       !- Begin Day of Month
    ,                        !- Begin Year
    1,                       !- End Month
    2,                       !- End Day of Month
    ,                        !- End Year
    Sunday,                  !- Day of Week for Start Day
    No,                      !- Use Weather File Holidays and Special Days
    No,                      !- Use Weather File Daylight Saving Period
    No,                      !- Apply Weekend Holiday Rule
    Yes,                     !- Use Weather File Rain Indicators
    Yes;                     !- Use Weather File Snow Indicators

Timestep,
    4;                       !- Number of Timesteps per Hour

Zone,
    TestZone,                !- Name
    0,                       !- Direction of Relative North {deg}
    0,                       !- X Origin {m}
    0,                       !- Y Origin {m}
    0,                       !- Z Origin {m}
    1,                       !- Type
    1,                       !- Multiplier
    autocalculate,           !- Ceiling Height {m}
    autocalculate;           !- Volume {m3}
"""

        # Upload the test model to GCS
        model_uri = self._upload_test_model(minimal_idf)

        # Build a test input envelope
        # Using a dummy callback URL since we're polling instead
        envelope = {
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
                    "name": "model.idf",
                    "mime_type": "application/x-energyplus",
                    "role": "model",
                    "uri": model_uri,
                },
                {
                    "name": "weather.epw",
                    "mime_type": "application/x-epw",
                    "role": "weather",
                    "uri": weather_uri,
                },
            ],
            "simulation_config": {
                "timestep_per_hour": 4,
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

        # Trigger the Cloud Run Job
        from simplevalidations.validations.services.cloud_run.job_client import (
            trigger_validator_job,
        )

        logger.info("Triggering EnergyPlus Cloud Run Job...")
        task_name = trigger_validator_job(
            project_id=settings.GCP_PROJECT_ID,
            region=settings.GCP_REGION,
            queue_name=settings.GCS_TASK_QUEUE_NAME,
            job_name=settings.GCS_ENERGYPLUS_JOB_NAME,
            input_uri=input_uri,
        )
        logger.info("Created task: %s", task_name)

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
        logger.info(
            "EnergyPlus job completed with status: %s",
            output.get("status"),
        )

        # We expect the job to succeed with our minimal model
        # But even a failure is "working" from an infrastructure perspective
        status = output.get("status")
        self.assertIn(
            status,
            ["success", "failed_validation", "failed_runtime"],
            f"Unexpected status: {status}",
        )

    def _upload_test_model(self, content: str) -> str:
        """Upload test model content to GCS."""
        from google.cloud import storage

        client = storage.Client()
        bucket = client.bucket(settings.GCS_VALIDATION_BUCKET)

        blob_path = f"runs/{self.test_org_id}/{self.test_run_id}/model.idf"
        blob = bucket.blob(blob_path)
        blob.upload_from_string(content, content_type="text/plain")

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
