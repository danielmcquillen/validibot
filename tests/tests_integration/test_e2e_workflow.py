"""
End-to-end workflow tests that verify the complete validation flow.

These tests exercise the full production path:
    1. Submit file via API
    2. Django creates ValidationRun
    3. Django launches Cloud Run Job (Jobs API)
    4. Job executes, calls back to Django worker (via WORKER_URL)
    5. Django processes callback, updates database
    6. Test polls API until run completes
    7. Verify status and findings

Unlike test_validator_jobs.py which tests Cloud Run Jobs in isolation (with
skip_callback=True), these tests verify the callback mechanism actually works.

Prerequisites:
    - E2E_TEST_API_URL: Deployed API URL (e.g., https://staging.validibot.com/api/v1)
    - E2E_TEST_API_TOKEN: Valid API token with workflow execution permission
    - E2E_TEST_WORKFLOW_ID: UUID of workflow with EnergyPlus validator step
    - E2E_TEST_WORKFLOW_EXPECTS_SUCCESS: Set to "false" if the test file should fail

These tests are opt-in and will skip if environment variables are not set.
Run them after deploying to staging to verify the full flow works.

Example:
    E2E_TEST_API_URL=https://staging.validibot.com/api/v1 \
    E2E_TEST_API_TOKEN=your-token \
    E2E_TEST_WORKFLOW_ID=workflow-uuid \
    pytest tests/tests_integration/test_e2e_workflow.py -v
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import requests
from django.conf import settings
from django.test import TestCase

logger = logging.getLogger(__name__)

# Configuration from environment
E2E_API_URL = os.getenv("E2E_TEST_API_URL", "")
E2E_API_TOKEN = os.getenv("E2E_TEST_API_TOKEN", "")
E2E_WORKFLOW_ID = os.getenv("E2E_TEST_WORKFLOW_ID", "")
E2E_EXPECTS_SUCCESS = (
    os.getenv("E2E_TEST_WORKFLOW_EXPECTS_SUCCESS", "true").lower() == "true"
)

# Polling configuration
E2E_POLL_INTERVAL_SECONDS = 10
E2E_TIMEOUT_SECONDS = 300  # 5 minutes


class WorkflowE2ETest(TestCase):
    """
    End-to-end tests for the complete validation workflow.

    These tests submit real files through the API and wait for completion,
    exercising the full Cloud Run Job -> callback -> database update path.
    """

    @classmethod
    def setUpClass(cls) -> None:
        """Check prerequisites and skip if not configured."""
        super().setUpClass()
        cls._check_prerequisites()

    @classmethod
    def _check_prerequisites(cls) -> None:
        """Verify all required environment variables are set."""
        logger.info("=" * 60)
        logger.info("E2E WORKFLOW TEST: Checking prerequisites")
        logger.info("=" * 60)

        missing = []
        if not E2E_API_URL:
            missing.append("E2E_TEST_API_URL")
        if not E2E_API_TOKEN:
            missing.append("E2E_TEST_API_TOKEN")
        if not E2E_WORKFLOW_ID:
            missing.append("E2E_TEST_WORKFLOW_ID")

        if missing:
            reason = f"E2E workflow test skipped: missing env vars {missing}"
            logger.warning(reason)
            raise cls.skipTest(cls, reason)

        logger.info("E2E_TEST_API_URL: %s", E2E_API_URL)
        logger.info("E2E_TEST_WORKFLOW_ID: %s", E2E_WORKFLOW_ID)
        logger.info("E2E_TEST_WORKFLOW_EXPECTS_SUCCESS: %s", E2E_EXPECTS_SUCCESS)
        logger.info("Prerequisites check: PASSED")

    def _get_auth_headers(self) -> dict:
        """Get authentication headers for API requests."""
        return {"Authorization": f"Bearer {E2E_API_TOKEN}"}

    def _submit_validation(
        self,
        file_path: Path,
        *,
        name: str | None = None,
    ) -> dict:
        """
        Submit a file for validation via the API.

        Args:
            file_path: Path to the file to validate
            name: Optional name for the submission

        Returns:
            API response containing run_id
        """
        url = f"{E2E_API_URL}/workflows/{E2E_WORKFLOW_ID}/start/"

        with file_path.open("rb") as f:
            files = {"file": (file_path.name, f)}
            data = {}
            if name:
                data["name"] = name

            logger.info("Submitting %s to %s", file_path.name, url)
            response = requests.post(
                url,
                headers=self._get_auth_headers(),
                files=files,
                data=data,
                timeout=60,
            )

        response.raise_for_status()
        return response.json()

    def _get_run_status(self, run_id: str) -> dict:
        """Get the current status of a validation run."""
        url = f"{E2E_API_URL}/validations/runs/{run_id}/"
        response = requests.get(
            url,
            headers=self._get_auth_headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def _wait_for_completion(
        self,
        run_id: str,
        *,
        timeout_seconds: int = E2E_TIMEOUT_SECONDS,
        poll_interval: int = E2E_POLL_INTERVAL_SECONDS,
    ) -> dict:
        """
        Poll until the validation run reaches a terminal status.

        Args:
            run_id: UUID of the validation run
            timeout_seconds: Maximum time to wait
            poll_interval: Seconds between polls

        Returns:
            Final run status

        Raises:
            TimeoutError: If run doesn't complete within timeout
        """
        terminal_statuses = {"SUCCEEDED", "FAILED", "CANCELED", "TIMED_OUT"}
        start_time = time.time()

        while time.time() - start_time < timeout_seconds:
            status_data = self._get_run_status(run_id)
            run_status = status_data.get("status", "UNKNOWN")
            elapsed = int(time.time() - start_time)

            logger.info(
                "Run %s status: %s (elapsed: %ds/%ds)",
                run_id[:8],
                run_status,
                elapsed,
                timeout_seconds,
            )

            if run_status in terminal_statuses:
                return status_data

            time.sleep(poll_interval)

        msg = f"Run {run_id} did not complete within {timeout_seconds}s"
        raise TimeoutError(msg)

    def _get_findings(self, run_id: str) -> list[dict]:
        """Get validation findings for a completed run."""
        url = f"{E2E_API_URL}/validations/runs/{run_id}/findings/"
        response = requests.get(
            url,
            headers=self._get_auth_headers(),
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def test_energyplus_workflow_completes_with_callback(self) -> None:
        """
        Test that an EnergyPlus workflow completes end-to-end.

        This test verifies:
        1. File submission via API works
        2. Workflow execution starts
        3. Cloud Run Job runs and calls back
        4. Run reaches terminal status
        5. Status can be retrieved via API
        """
        # Use the example epJSON file
        test_file = (
            Path(settings.BASE_DIR)
            / "tests"
            / "data"
            / "energyplus"
            / "example_epjson.json"
        )

        if not test_file.exists():
            self.skipTest(f"Test file not found: {test_file}")

        # Submit the file
        logger.info("=" * 60)
        logger.info("Starting E2E workflow test")
        logger.info("=" * 60)

        result = self._submit_validation(
            test_file,
            name="E2E Test Submission",
        )

        run_id = result.get("run_id")
        self.assertIsNotNone(run_id, f"No run_id in response: {result}")
        logger.info("Created validation run: %s", run_id)

        # Wait for completion (this exercises the callback path!)
        try:
            final_status = self._wait_for_completion(run_id)
        except TimeoutError as e:
            self.fail(str(e))

        # Log the result
        status = final_status.get("status")
        error_category = final_status.get("error_category", "")
        user_error = final_status.get("user_friendly_error", "")

        logger.info("=" * 60)
        logger.info("E2E workflow test completed")
        logger.info("Status: %s", status)
        if error_category:
            logger.info("Error category: %s", error_category)
        if user_error:
            logger.info("Error message: %s", user_error)
        logger.info("=" * 60)

        # Verify we got a terminal status (callback worked!)
        self.assertIn(
            status,
            ["SUCCEEDED", "FAILED", "CANCELED", "TIMED_OUT"],
            f"Run did not reach terminal status: {status}",
        )

        # Verify expected outcome
        if E2E_EXPECTS_SUCCESS:
            self.assertEqual(
                status,
                "SUCCEEDED",
                f"Expected SUCCEEDED but got {status}. "
                f"Error: {user_error or final_status.get('error', 'unknown')}",
            )
        else:
            # We expect failure (validation found issues)
            self.assertEqual(
                status,
                "FAILED",
                f"Expected FAILED but got {status}",
            )

        # Try to get findings (optional - don't fail if endpoint doesn't exist)
        try:
            findings = self._get_findings(run_id)
            logger.info("Found %d findings", len(findings))
            for finding in findings[:5]:  # Log first 5
                severity = finding.get("severity", "?")
                message = finding.get("message", "")[:80]
                logger.info("  [%s] %s", severity, message)
        except requests.HTTPError as e:
            logger.warning("Could not retrieve findings: %s", e)

    def test_api_connectivity(self) -> None:
        """
        Verify we can connect to the API and authenticate.

        This is a basic smoke test that runs before the full E2E test.
        """
        # Try to list validation runs
        url = f"{E2E_API_URL}/validations/runs/"
        response = requests.get(
            url,
            headers=self._get_auth_headers(),
            timeout=30,
        )

        self.assertEqual(
            response.status_code,
            200,
            f"API connectivity failed: {response.status_code} - {response.text[:200]}",
        )
        logger.info("API connectivity: OK")

    def test_workflow_exists(self) -> None:
        """
        Verify the configured workflow exists and is accessible.

        This validates the E2E_TEST_WORKFLOW_ID is correct.
        """
        url = f"{E2E_API_URL}/workflows/{E2E_WORKFLOW_ID}/"
        response = requests.get(
            url,
            headers=self._get_auth_headers(),
            timeout=30,
        )

        self.assertEqual(
            response.status_code,
            200,
            f"Workflow {E2E_WORKFLOW_ID} not accessible: {response.status_code}",
        )

        workflow = response.json()
        logger.info("Workflow found: %s", workflow.get("name", "unnamed"))
