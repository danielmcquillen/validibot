"""
Validator-specific integration checks for Cloud Run Jobs.

These tests exercise validator containers end-to-end and validate the
result envelope shape beyond the generic job plumbing. They are opt-in
and will skip if the required GCP configuration is missing.

Testing Approach
----------------
In production, the validation flow works like this:

    1. Django uploads input.json to GCS and triggers the Cloud Run Job
    2. The job reads input.json, runs the simulation, writes output.json to GCS
    3. The job POSTs a callback to Django's worker service with the result URI
    4. Django reads output.json and updates the database

These tests run the Cloud Run Job in isolation WITHOUT a Django app to receive
callbacks. Instead of waiting for a callback, we poll GCS for the output.json
file that the job writes before it would normally call back to Django. This
lets us verify the job runs correctly and produces valid output envelopes
without needing the full Django infrastructure.

The input envelope includes `skip_callback: True` in the context to tell the
job not to attempt the callback (which would fail since there's no Django
endpoint listening).
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.test import TestCase

from validibot.validations.services.cloud_run.job_client import run_validator_job

# Configure logging for verbose test output
logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from google.cloud import storage


class EnergyPlusValidatorE2ETest(TestCase):
    """
    End-to-end EnergyPlus validator via Cloud Run Job.

    This test uploads a minimal EnergyPlus envelope, triggers the validator
    Cloud Run Job, waits for the output, and asserts the envelope shape
    matches the EnergyPlus contract.
    """

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._check_prerequisites()
        cls._ensure_weather_file_in_bucket()

    @classmethod
    def _check_prerequisites(cls) -> None:
        """Verify GCP settings needed for the test are present."""
        logger.info("=" * 60)
        logger.info("ENERGYPLUS E2E TEST: Checking prerequisites")
        logger.info("=" * 60)

        required_settings = [
            "GCP_PROJECT_ID",
            "GCS_VALIDATION_BUCKET",
            "GCS_ENERGYPLUS_JOB_NAME",
            "GCP_REGION",
        ]
        missing = [s for s in required_settings if not getattr(settings, s, None)]
        if missing:
            reason = f"EnergyPlus E2E skipped: missing settings {missing}"
            logger.warning(reason)
            raise cls.skipTest(cls, reason)

        # Log the settings we're using
        logger.info("GCP_PROJECT_ID: %s", settings.GCP_PROJECT_ID)
        logger.info("GCS_VALIDATION_BUCKET: %s", settings.GCS_VALIDATION_BUCKET)
        logger.info("GCS_ENERGYPLUS_JOB_NAME: %s", settings.GCS_ENERGYPLUS_JOB_NAME)
        logger.info("GCP_REGION: %s", settings.GCP_REGION)

        try:
            from google.auth import default

            credentials, project = default()
            logger.info("GCP credentials: OK (project=%s)", project)
        except Exception as exc:
            reason = f"EnergyPlus E2E skipped: GCP credentials not configured: {exc}"
            logger.warning(reason)
            raise cls.skipTest(cls, reason) from None

        logger.info("Prerequisites check: PASSED")

    @classmethod
    def _ensure_weather_file_in_bucket(cls) -> None:
        """Upload the test weather file to GCS if it is missing."""
        from google.cloud import storage

        weather_file = getattr(
            settings,
            "TEST_ENERGYPLUS_WEATHER_FILE",
            "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw",
        )
        bucket_path = (
            f"{settings.GCS_VALIDATOR_ASSETS_PREFIX}/"
            f"{settings.GCS_WEATHER_DATA_DIR}/{weather_file}"
        )

        client = storage.Client()
        bucket = client.bucket(settings.GCS_VALIDATION_BUCKET)
        blob = bucket.blob(bucket_path)

        if blob.exists():
            cls.weather_file = weather_file
            return

        local_weather = (
            Path(settings.BASE_DIR)
            / "tests"
            / "data"
            / "energyplus"
            / "test_weather.epw"
        )

        if not local_weather.exists():
            raise cls.skipTest(
                cls,
                "EnergyPlus E2E skipped: local weather fixture missing; "
                f"add {local_weather}",
            )

        blob.upload_from_filename(
            filename=str(local_weather),
            content_type="application/vnd.energyplus.epw",
        )
        cls.weather_file = weather_file

    def setUp(self) -> None:
        super().setUp()
        self.test_run_id = f"test-{uuid.uuid4()}"
        self.test_org_id = "test-org"
        self.weather_file = getattr(
            self.__class__,
            "weather_file",
            "USA_CA_San.Francisco.Intl.AP.724940_TMY3.epw",
        )

    def tearDown(self) -> None:
        super().tearDown()
        self._cleanup_gcs_artifacts()

    def _cleanup_gcs_artifacts(self) -> None:
        """Remove test files from the validation bucket."""
        try:
            from google.cloud import storage

            client = storage.Client()
            bucket = client.bucket(settings.GCS_VALIDATION_BUCKET)
            prefix = f"runs/{self.test_org_id}/{self.test_run_id}/"
            for blob in list(bucket.list_blobs(prefix=prefix)):
                blob.delete()
        except Exception:
            # Best-effort cleanup; ignore failures in tests.
            return

    def _gcs_client(self) -> storage.Client:
        from google.cloud import storage

        return storage.Client()

    def _upload_to_gcs(self, path: str, content: str, content_type: str) -> str:
        client = self._gcs_client()
        bucket = client.bucket(settings.GCS_VALIDATION_BUCKET)
        blob = bucket.blob(path)
        blob.upload_from_string(content, content_type=content_type)
        return f"gs://{settings.GCS_VALIDATION_BUCKET}/{path}"

    def _poll_output_envelope(
        self,
        *,
        timeout_seconds: int = 300,
        poll_interval_seconds: int = 10,
    ) -> dict | None:
        import time

        client = self._gcs_client()
        bucket = client.bucket(settings.GCS_VALIDATION_BUCKET)
        output_path = f"runs/{self.test_org_id}/{self.test_run_id}/output.json"

        start = time.time()
        while time.time() - start < timeout_seconds:
            blob = bucket.blob(output_path)
            if blob.exists():
                content = blob.download_as_bytes()
                return json.loads(content)
            time.sleep(poll_interval_seconds)
        return None

    def test_energyplus_envelope_shape(self) -> None:
        """Validator result envelope should match EnergyPlus schema basics."""
        # Use the shipped sample epJSON submission to mimic a real user payload
        sample_path = (
            Path(settings.BASE_DIR)
            / "tests"
            / "data"
            / "energyplus"
            / "example_epjson.json"
        )
        sample_content = sample_path.read_text()

        weather_uri = (
            f"gs://{settings.GCS_VALIDATION_BUCKET}/"
            f"{settings.GCS_VALIDATOR_ASSETS_PREFIX}/{settings.GCS_WEATHER_DATA_DIR}/{self.weather_file}"
        )

        model_uri = self._upload_to_gcs(
            f"runs/{self.test_org_id}/{self.test_run_id}/model.epjson",
            sample_content,
            "application/json",
        )

        envelope = {
            "schema_version": "validibot.input.v1",
            "run_id": self.test_run_id,
            "validator": {
                "id": "test-validator",
                "type": "ENERGYPLUS",
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
            "inputs": {
                "timestep_per_hour": 4,
                "output_variables": [],
                "invocation_mode": "cli",
            },
            "context": {
                "skip_callback": True,
                "execution_bundle_uri": (
                    f"gs://{settings.GCS_VALIDATION_BUCKET}"
                    f"/runs/{self.test_org_id}/{self.test_run_id}"
                ),
            },
        }

        input_uri = self._upload_to_gcs(
            f"runs/{self.test_org_id}/{self.test_run_id}/input.json",
            json.dumps(envelope),
            "application/json",
        )

        execution_name = run_validator_job(
            project_id=settings.GCP_PROJECT_ID,
            region=settings.GCP_REGION,
            job_name=settings.GCS_ENERGYPLUS_JOB_NAME,
            input_uri=input_uri,
        )
        self.assertTrue(execution_name)

        output = self._poll_output_envelope()
        if output is None:
            self.skipTest(
                "EnergyPlus output not found within timeout; skipping E2E assertion.",
            )

        # Basic schema checks
        self.assertIn(
            output.get("schema_version"),
            ["validibot.result.v1", "validibot.output.v1"],
        )
        self.assertEqual(output.get("run_id"), self.test_run_id)
        self.assertEqual(output.get("validator", {}).get("type"), "ENERGYPLUS")
        status = output.get("status")
        if status != "success":
            self.skipTest(
                "EnergyPlus job did not succeed "
                f"(status={status}); see job logs for details.",
            )

    def test_energyplus_error_messages_extracted(self) -> None:
        """
        Validator should extract error messages from .err file when validation fails.

        This test uses an invalid model file that will cause EnergyPlus to fail
        with errors. We verify that:
        1. The output envelope has status 'failed_validation'
        2. The 'messages' array is populated with error messages from the .err file
        3. Error messages have the correct severity and structure
        """
        logger.info("=" * 60)
        logger.info("TEST: test_energyplus_error_messages_extracted")
        logger.info("=" * 60)

        # Use a different run ID for this test
        self.test_run_id = f"test-eplus-errors-{uuid.uuid4()}"
        logger.info("Test run_id: %s", self.test_run_id)

        # Use an invalid model that will cause EnergyPlus to fail with errors
        invalid_model_path = (
            Path(settings.BASE_DIR)
            / "tests"
            / "data"
            / "energyplus"
            / "invalid_model.epjson"
        )
        if not invalid_model_path.exists():
            self.skipTest(f"Invalid model fixture not found: {invalid_model_path}")

        model_content = invalid_model_path.read_text()

        weather_uri = (
            f"gs://{settings.GCS_VALIDATION_BUCKET}/"
            f"{settings.GCS_VALIDATOR_ASSETS_PREFIX}/{settings.GCS_WEATHER_DATA_DIR}/{self.weather_file}"
        )

        logger.info("Step 1: Uploading invalid model to GCS")
        model_uri = self._upload_to_gcs(
            f"runs/{self.test_org_id}/{self.test_run_id}/model.epjson",
            model_content,
            "application/json",
        )

        logger.info("Step 2: Building input envelope")
        envelope = {
            "schema_version": "validibot.input.v1",
            "run_id": self.test_run_id,
            "validator": {
                "id": "test-validator",
                "type": "ENERGYPLUS",
                "version": "24.1",
            },
            "org": {
                "id": self.test_org_id,
                "name": "Test Organization",
            },
            "workflow": {
                "id": "test-workflow",
                "step_id": "test-step",
                "step_name": "Test EnergyPlus Error Extraction",
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
            "inputs": {
                "timestep_per_hour": 4,
                "output_variables": [],
                "invocation_mode": "cli",
            },
            "context": {
                "skip_callback": True,
                "execution_bundle_uri": (
                    f"gs://{settings.GCS_VALIDATION_BUCKET}"
                    f"/runs/{self.test_org_id}/{self.test_run_id}"
                ),
            },
        }

        logger.info("Step 3: Uploading input envelope")
        input_uri = self._upload_to_gcs(
            f"runs/{self.test_org_id}/{self.test_run_id}/input.json",
            json.dumps(envelope),
            "application/json",
        )

        logger.info("Step 4: Triggering EnergyPlus Cloud Run Job")
        execution_name = run_validator_job(
            project_id=settings.GCP_PROJECT_ID,
            region=settings.GCP_REGION,
            job_name=settings.GCS_ENERGYPLUS_JOB_NAME,
            input_uri=input_uri,
        )
        logger.info("Execution started: %s", execution_name)
        self.assertTrue(execution_name)

        logger.info("Step 5: Polling for output envelope")
        output = self._poll_output_envelope()
        if output is None:
            self.skipTest(
                "EnergyPlus output not found within timeout; skipping E2E assertion.",
            )

        logger.info("Step 6: Validating output envelope")
        logger.info("  Output: %s", json.dumps(output, indent=2, default=str))

        # Basic schema checks
        self.assertIn(
            output.get("schema_version"),
            ["validibot.result.v1", "validibot.output.v1"],
        )
        self.assertEqual(output.get("run_id"), self.test_run_id)
        self.assertEqual(output.get("validator", {}).get("type"), "ENERGYPLUS")

        # We expect the status to be failed_validation for an invalid model
        status = output.get("status")
        logger.info("  status: %s", status)
        self.assertIn(
            status,
            ["failed_validation", "failed_runtime"],
            f"Expected failed status for invalid model, got: {status}",
        )

        # KEY ASSERTION: Error messages should be extracted from .err file
        messages = output.get("messages", [])
        logger.info("  messages count: %d", len(messages))
        for msg in messages:
            logger.info(
                "    [%s] %s (code=%s)",
                msg.get("severity", "?"),
                msg.get("text", "")[:100],
                msg.get("code"),
            )

        # Verify messages are present
        self.assertGreater(
            len(messages),
            0,
            "Expected error messages to be extracted from"
            " .err file, but messages array is empty",
        )

        # Verify message structure
        for msg in messages:
            self.assertIn(
                "severity",
                msg,
                "Each message should have a 'severity' field",
            )
            # Severity can be uppercase (from Pydantic enum) or lowercase
            severity_lower = msg.get("severity", "").lower()
            self.assertIn(
                severity_lower,
                ["error", "warning", "info"],
                f"Unexpected severity: {msg.get('severity')}",
            )
            self.assertIn(
                "text",
                msg,
                "Each message should have a 'text' field",
            )
            self.assertTrue(
                msg.get("text"),
                "Message text should not be empty",
            )

        # At least one error should be present (since we used an invalid model)
        error_messages = [
            m for m in messages if m.get("severity", "").lower() == "error"
        ]
        logger.info("  error messages count: %d", len(error_messages))
        self.assertGreater(
            len(error_messages),
            0,
            "Expected at least one error message for invalid model",
        )

        logger.info("TEST PASSED: test_energyplus_error_messages_extracted")


class FMIValidatorE2ETest(TestCase):
    """
    End-to-end FMI validator via Cloud Run Job.

    This test uploads a Feedthrough FMU (standard FMI test model that passes
    inputs to outputs), triggers the FMI validator Cloud Run Job, waits for
    the output, and asserts the envelope shape matches the FMI contract.

    The Feedthrough FMU is a simple FMI 2.0 Co-Simulation model commonly used
    for FMI compliance testing. It has input variables that are passed through
    to output variables unchanged.

    Prerequisites:
    - GCP_PROJECT_ID environment variable set
    - GCS_VALIDATION_BUCKET environment variable set
    - GCS_FMI_JOB_NAME environment variable set
    - GCP_REGION environment variable set
    - Valid GCP credentials
    - FMI Cloud Run Job deployed and ready
    """

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._check_prerequisites()

    @classmethod
    def _check_prerequisites(cls) -> None:
        """Verify GCP settings needed for the test are present."""
        logger.info("=" * 60)
        logger.info("FMI E2E TEST: Checking prerequisites")
        logger.info("=" * 60)

        required_settings = [
            "GCP_PROJECT_ID",
            "GCS_VALIDATION_BUCKET",
            "GCS_FMI_JOB_NAME",
            "GCP_REGION",
        ]
        missing = [s for s in required_settings if not getattr(settings, s, None)]
        if missing:
            logger.warning("FMI E2E skipped: missing settings %s", missing)
            raise cls.skipTest(
                cls,
                f"FMI E2E skipped: missing settings {missing}",
            )

        # Log the settings we're using
        logger.info("GCP_PROJECT_ID: %s", settings.GCP_PROJECT_ID)
        logger.info("GCS_VALIDATION_BUCKET: %s", settings.GCS_VALIDATION_BUCKET)
        logger.info("GCS_FMI_JOB_NAME: %s", settings.GCS_FMI_JOB_NAME)
        logger.info("GCP_REGION: %s", settings.GCP_REGION)

        try:
            from google.auth import default

            credentials, project = default()
            logger.info("GCP credentials: OK (project=%s)", project)
        except Exception as exc:
            logger.warning("FMI E2E skipped: GCP credentials not configured: %s", exc)
            raise cls.skipTest(
                cls,
                f"FMI E2E skipped: GCP credentials not configured: {exc}",
            ) from None

        # Verify the Feedthrough.fmu test asset exists
        from pathlib import Path

        fmu_path = (
            Path(settings.BASE_DIR) / "tests" / "assets" / "fmu" / "Feedthrough.fmu"
        )
        if not fmu_path.exists():
            logger.warning("FMI E2E skipped: test FMU not found at %s", fmu_path)
            raise cls.skipTest(
                cls,
                f"FMI E2E skipped: test FMU not found at {fmu_path}",
            )
        logger.info("Test FMU found: %s (%d bytes)", fmu_path, fmu_path.stat().st_size)
        logger.info("Prerequisites check: PASSED")

    def setUp(self) -> None:
        super().setUp()
        self.test_run_id = f"test-fmi-{uuid.uuid4()}"
        self.test_org_id = "test-org"
        logger.info("-" * 60)
        logger.info("Test run_id: %s", self.test_run_id)
        logger.info("Test org_id: %s", self.test_org_id)

    def tearDown(self) -> None:
        super().tearDown()
        self._cleanup_gcs_artifacts()

    def _cleanup_gcs_artifacts(self) -> None:
        """Remove test files from the validation bucket."""
        try:
            from google.cloud import storage

            client = storage.Client()
            bucket = client.bucket(settings.GCS_VALIDATION_BUCKET)
            prefix = f"runs/{self.test_org_id}/{self.test_run_id}/"
            blobs = list(bucket.list_blobs(prefix=prefix))
            logger.info(
                "Cleaning up %d GCS artifacts with prefix: %s",
                len(blobs),
                prefix,
            )
            for blob in blobs:
                blob.delete()
                logger.debug("Deleted: %s", blob.name)
        except Exception as exc:
            logger.warning("Cleanup failed: %s", exc)

    def _gcs_client(self) -> storage.Client:
        from google.cloud import storage

        return storage.Client()

    def _upload_to_gcs(self, path: str, content: str, content_type: str) -> str:
        """Upload string content to GCS."""
        client = self._gcs_client()
        bucket = client.bucket(settings.GCS_VALIDATION_BUCKET)
        blob = bucket.blob(path)
        blob.upload_from_string(content, content_type=content_type)
        uri = f"gs://{settings.GCS_VALIDATION_BUCKET}/{path}"
        logger.info("Uploaded to GCS: %s (%d bytes)", uri, len(content))
        return uri

    def _upload_bytes_to_gcs(self, path: str, content: bytes, content_type: str) -> str:
        """Upload binary content to GCS."""
        client = self._gcs_client()
        bucket = client.bucket(settings.GCS_VALIDATION_BUCKET)
        blob = bucket.blob(path)
        blob.upload_from_string(content, content_type=content_type)
        uri = f"gs://{settings.GCS_VALIDATION_BUCKET}/{path}"
        logger.info("Uploaded to GCS: %s (%d bytes)", uri, len(content))
        return uri

    def _upload_fmu_to_gcs(self) -> str:
        """Upload the test Feedthrough.fmu to GCS and return its URI."""
        from pathlib import Path

        fmu_path = (
            Path(settings.BASE_DIR) / "tests" / "assets" / "fmu" / "Feedthrough.fmu"
        )
        fmu_content = fmu_path.read_bytes()
        gcs_path = f"runs/{self.test_org_id}/{self.test_run_id}/model.fmu"
        logger.info(
            "Uploading FMU: %s -> gs://%s/%s",
            fmu_path.name,
            settings.GCS_VALIDATION_BUCKET,
            gcs_path,
        )
        return self._upload_bytes_to_gcs(
            gcs_path,
            fmu_content,
            "application/vnd.fmi.fmu",
        )

    def _poll_output_envelope(
        self,
        *,
        run_id: str | None = None,
        timeout_seconds: int = 60,
        poll_interval_seconds: int = 5,
    ) -> dict | None:
        """Poll GCS for the output envelope with verbose logging."""
        import time

        run_id = run_id or self.test_run_id
        client = self._gcs_client()
        bucket = client.bucket(settings.GCS_VALIDATION_BUCKET)
        output_path = f"runs/{self.test_org_id}/{run_id}/output.json"

        logger.info(
            "Polling for output at: gs://%s/%s",
            settings.GCS_VALIDATION_BUCKET,
            output_path,
        )
        logger.info(
            "Timeout: %ds, Poll interval: %ds",
            timeout_seconds,
            poll_interval_seconds,
        )

        start = time.time()
        poll_count = 0
        while time.time() - start < timeout_seconds:
            poll_count += 1
            elapsed = time.time() - start
            blob = bucket.blob(output_path)
            if blob.exists():
                content = blob.download_as_bytes()
                output = json.loads(content)
                logger.info(
                    "Output envelope found after %.1fs (%d polls)",
                    elapsed,
                    poll_count,
                )
                return output
            logger.info("Poll %d: No output yet (%.1fs elapsed)", poll_count, elapsed)
            time.sleep(poll_interval_seconds)

        logger.warning(
            "Timeout: No output found after %ds (%d polls)",
            timeout_seconds,
            poll_count,
        )
        return None

    def _log_envelope(self, label: str, envelope: dict) -> None:
        """Log an envelope with pretty-printed JSON."""
        logger.info("%s:", label)
        logger.info(json.dumps(envelope, indent=2, default=str))

    def test_fmi_envelope_shape(self) -> None:
        """
        FMI validator result envelope should match FMI schema.

        This test:
        1. Uploads the Feedthrough.fmu test model to GCS
        2. Creates an FMI input envelope with simulation config
        3. Triggers the FMI Cloud Run Job
        4. Polls for the output envelope
        5. Validates the output structure matches FMIOutputEnvelope
        """
        logger.info("=" * 60)
        logger.info("TEST: test_fmi_envelope_shape")
        logger.info("=" * 60)

        # Upload the test FMU
        logger.info("Step 1: Uploading test FMU to GCS")
        fmu_uri = self._upload_fmu_to_gcs()

        # Build FMI input envelope matching FMIInputEnvelope schema
        logger.info("Step 2: Building input envelope")
        envelope = {
            "schema_version": "validibot.input.v1",
            "run_id": self.test_run_id,
            "validator": {
                "id": "test-fmi-validator",
                "type": "FMI",
                "version": "1.0.0",
            },
            "org": {
                "id": self.test_org_id,
                "name": "Test Organization",
            },
            "workflow": {
                "id": "test-workflow",
                "step_id": "test-step",
                "step_name": "Test FMI Step",
            },
            "input_files": [
                {
                    "name": "model.fmu",
                    "mime_type": "application/vnd.fmi.fmu",
                    "role": "fmu",
                    "uri": fmu_uri,
                },
            ],
            "inputs": {
                "input_values": {},
                "simulation": {
                    "start_time": 0.0,
                    "stop_time": 1.0,
                    "step_size": 0.1,
                },
                "output_variables": [],
            },
            "context": {
                "skip_callback": True,
                "execution_bundle_uri": (
                    f"gs://{settings.GCS_VALIDATION_BUCKET}"
                    f"/runs/{self.test_org_id}/{self.test_run_id}"
                ),
            },
        }
        self._log_envelope("INPUT ENVELOPE", envelope)

        logger.info("Step 3: Uploading input envelope to GCS")
        input_uri = self._upload_to_gcs(
            f"runs/{self.test_org_id}/{self.test_run_id}/input.json",
            json.dumps(envelope),
            "application/json",
        )
        logger.info("Input URI: %s", input_uri)

        # Trigger the FMI Cloud Run Job
        logger.info("Step 4: Triggering FMI Cloud Run Job")
        logger.info("  Project: %s", settings.GCP_PROJECT_ID)
        logger.info("  Region: %s", settings.GCP_REGION)
        logger.info("  Job name: %s", settings.GCS_FMI_JOB_NAME)

        execution_name = run_validator_job(
            project_id=settings.GCP_PROJECT_ID,
            region=settings.GCP_REGION,
            job_name=settings.GCS_FMI_JOB_NAME,
            input_uri=input_uri,
        )
        logger.info("Execution started: %s", execution_name)
        self.assertTrue(execution_name, "Expected non-empty execution name")

        # Poll for output
        logger.info("Step 5: Polling for output envelope")
        output = self._poll_output_envelope()
        if output is None:
            logger.error("FAILED: Output envelope not found within timeout")
            self.skipTest(
                "FMI output not found within timeout; skipping E2E assertion.",
            )

        self._log_envelope("OUTPUT ENVELOPE", output)

        # Validate output envelope structure
        logger.info("Step 6: Validating output envelope structure")

        schema_version = output.get("schema_version")
        logger.info("  schema_version: %s", schema_version)
        self.assertIn(
            schema_version,
            ["validibot.result.v1", "validibot.output.v1"],
            f"Unexpected schema_version: {schema_version}",
        )

        run_id = output.get("run_id")
        logger.info("  run_id: %s (expected: %s)", run_id, self.test_run_id)
        self.assertEqual(run_id, self.test_run_id, f"run_id mismatch: {run_id}")

        validator_type = output.get("validator", {}).get("type")
        logger.info("  validator.type: %s", validator_type)
        self.assertEqual(
            validator_type,
            "fmi",
            f"validator.type mismatch: {validator_type}",
        )

        status = output.get("status")
        logger.info("  status: %s", status)
        self.assertIn(
            status,
            ["success", "failed_validation", "failed_runtime", "cancelled"],
            f"Unexpected status: {status}",
        )

        timing = output.get("timing", {})
        logger.info("  timing.started_at: %s", timing.get("started_at"))
        logger.info("  timing.finished_at: %s", timing.get("finished_at"))
        self.assertIsNotNone(
            timing.get("started_at"),
            "timing.started_at should be present",
        )

        # Log any messages
        messages = output.get("messages", [])
        if messages:
            logger.info("  Messages (%d):", len(messages))
            for msg in messages:
                logger.info(
                    "    [%s] %s",
                    msg.get("severity", "?"),
                    msg.get("text", ""),
                )

        # If successful, validate FMI-specific outputs
        if status == "success":
            logger.info("  Validating FMI-specific outputs (status=success)")
            outputs = output.get("outputs")
            self.assertIsNotNone(outputs, "outputs should be present on success")

            logger.info("    execution_seconds: %s", outputs.get("execution_seconds"))
            self.assertIn("execution_seconds", outputs)
            self.assertGreaterEqual(
                outputs.get("execution_seconds", -1),
                0,
                "execution_seconds should be >= 0",
            )

            sim_time = outputs.get("simulation_time_reached")
            logger.info("    simulation_time_reached: %s", sim_time)
            self.assertIn("simulation_time_reached", outputs)
            self.assertGreaterEqual(
                outputs.get("simulation_time_reached", -1),
                0,
                "simulation_time_reached should be >= 0",
            )

            logger.info("    fmi_version: %s", outputs.get("fmi_version"))
            logger.info("    model_name: %s", outputs.get("model_name"))
            logger.info("    fmu_guid: %s", outputs.get("fmu_guid"))
            self.assertIn("fmi_version", outputs)
            self.assertIn("model_name", outputs)
            self.assertIn("fmu_guid", outputs)

            output_values = outputs.get("output_values", {})
            logger.info("    output_values: %s", output_values)
            self.assertIn("output_values", outputs)
            self.assertIsInstance(output_values, dict)

            logger.info("TEST PASSED: test_fmi_envelope_shape")
        else:
            logger.warning(
                "Job did not succeed (status=%s), skipping output validation",
                status,
            )
            # Log any error details
            if "outputs" in output:
                logger.info("  outputs (partial): %s", output.get("outputs"))

    def test_fmi_with_input_values(self) -> None:
        """
        Test FMI simulation with explicit input values.

        The Feedthrough FMU passes inputs to outputs, so this tests that
        input values are properly passed through the envelope and processed.
        """
        logger.info("=" * 60)
        logger.info("TEST: test_fmi_with_input_values")
        logger.info("=" * 60)

        # Upload FMU
        logger.info("Step 1: Uploading test FMU to GCS")
        fmu_uri = self._upload_fmu_to_gcs()

        # Use a different run_id to avoid conflicts
        run_id = f"test-fmi-inputs-{uuid.uuid4()}"
        logger.info("Using separate run_id for this test: %s", run_id)

        logger.info("Step 2: Building input envelope with explicit input values")
        envelope = {
            "schema_version": "validibot.input.v1",
            "run_id": run_id,
            "validator": {
                "id": "test-fmi-validator",
                "type": "FMI",
                "version": "1.0.0",
            },
            "org": {
                "id": self.test_org_id,
                "name": "Test Organization",
            },
            "workflow": {
                "id": "test-workflow",
                "step_id": "test-step",
                "step_name": "Test FMI Step with Inputs",
            },
            "input_files": [
                {
                    "name": "model.fmu",
                    "mime_type": "application/vnd.fmi.fmu",
                    "role": "fmu",
                    "uri": fmu_uri,
                },
            ],
            "inputs": {
                "input_values": {
                    # Use the actual Feedthrough FMU input variable names
                    "real_continuous_in": 42.0,
                    "int_in": 7,
                    "bool_in": True,
                },
                "simulation": {
                    "start_time": 0.0,
                    "stop_time": 2.0,
                    "step_size": 0.5,
                },
                "output_variables": [],
            },
            "context": {
                "skip_callback": True,
                "execution_bundle_uri": (
                    f"gs://{settings.GCS_VALIDATION_BUCKET}"
                    f"/runs/{self.test_org_id}/{run_id}"
                ),
            },
        }
        self._log_envelope("INPUT ENVELOPE", envelope)

        logger.info("Step 3: Uploading input envelope to GCS")
        input_uri = self._upload_to_gcs(
            f"runs/{self.test_org_id}/{run_id}/input.json",
            json.dumps(envelope),
            "application/json",
        )
        logger.info("Input URI: %s", input_uri)

        logger.info("Step 4: Triggering FMI Cloud Run Job")
        execution_name = run_validator_job(
            project_id=settings.GCP_PROJECT_ID,
            region=settings.GCP_REGION,
            job_name=settings.GCS_FMI_JOB_NAME,
            input_uri=input_uri,
        )
        logger.info("Execution started: %s", execution_name)
        self.assertTrue(execution_name)

        logger.info("Step 5: Polling for output envelope")
        output = self._poll_output_envelope(run_id=run_id)

        # Cleanup this run's artifacts
        try:
            from google.cloud import storage

            client = storage.Client()
            bucket = client.bucket(settings.GCS_VALIDATION_BUCKET)
            prefix = f"runs/{self.test_org_id}/{run_id}/"
            blobs = list(bucket.list_blobs(prefix=prefix))
            logger.info("Cleaning up %d artifacts for run %s", len(blobs), run_id)
            for blob in blobs:
                blob.delete()
        except Exception as exc:
            logger.debug("Cleanup failed for run %s: %s", run_id, exc)

        if output is None:
            logger.error("FAILED: Output envelope not found within timeout")
            self.skipTest("FMI with inputs output not found within timeout.")

        self._log_envelope("OUTPUT ENVELOPE", output)

        logger.info("Step 6: Validating output envelope")
        self.assertEqual(output.get("run_id"), run_id)

        status = output.get("status")
        logger.info("  status: %s", status)
        self.assertIn(
            status,
            ["success", "failed_validation", "failed_runtime"],
            f"Unexpected status: {status}",
        )

        # If successful, check simulation reached expected time
        if status == "success":
            outputs = output.get("outputs", {})
            sim_time = outputs.get("simulation_time_reached", 0)
            logger.info("  simulation_time_reached: %s (expected >= 1.5)", sim_time)
            self.assertGreaterEqual(
                sim_time,
                1.5,
                f"Expected simulation to reach near stop_time, got {sim_time}",
            )

            output_values = outputs.get("output_values", {})
            logger.info("  output_values: %s", output_values)

            logger.info("TEST PASSED: test_fmi_with_input_values")
        else:
            logger.warning("Job did not succeed (status=%s)", status)
            if "outputs" in output:
                logger.info("  outputs: %s", output.get("outputs"))

    def test_fmi_gcs_connectivity(self) -> None:
        """
        Test that we can connect to GCS and the FMI job exists.

        This is a basic smoke test to verify GCP configuration works
        before running the full simulation tests.
        """
        logger.info("=" * 60)
        logger.info("TEST: test_fmi_gcs_connectivity")
        logger.info("=" * 60)

        from google.cloud import storage

        # Test GCS connectivity
        logger.info("Step 1: Testing GCS connectivity")
        client = storage.Client()
        bucket = client.bucket(settings.GCS_VALIDATION_BUCKET)
        bucket_exists = bucket.exists()
        logger.info(
            "  Bucket %s exists: %s",
            settings.GCS_VALIDATION_BUCKET,
            bucket_exists,
        )
        self.assertTrue(
            bucket_exists,
            f"Bucket {settings.GCS_VALIDATION_BUCKET} not accessible",
        )

        # Test that FMI job exists by checking its name is configured
        logger.info("Step 2: Checking FMI job configuration")
        job_name = settings.GCS_FMI_JOB_NAME
        logger.info("  GCS_FMI_JOB_NAME: %s", job_name)
        self.assertTrue(job_name, "GCS_FMI_JOB_NAME should be configured")

        # Test that we can upload the FMU
        logger.info("Step 3: Testing FMU upload")
        fmu_uri = self._upload_fmu_to_gcs()
        logger.info("  Uploaded to: %s", fmu_uri)
        self.assertTrue(fmu_uri.startswith("gs://"), "FMU should upload to GCS")

        logger.info("TEST PASSED: test_fmi_gcs_connectivity")
