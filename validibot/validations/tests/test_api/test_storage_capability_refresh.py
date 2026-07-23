"""Tests for terminal-fenced renewal of validator GCS capabilities.

Long-running Cloud Run jobs may outlive the first short-lived storage token.
Renewal must require the attempt's secret callback proof, return a prefix-
identical token without caching, and stop as soon as the durable attempt is
terminal. These tests call the thin view directly so worker OIDC transport is
covered independently by the shared worker-auth test suite.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase
from django.test import override_settings

from validibot.validations.api.storage_capabilities import (
    ValidationStorageCapabilityRefreshView,
)
from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.services.cloud_run.gcs_runtime_capabilities import (
    AttemptGCSRuntimeCapability,
)
from validibot.validations.services.execution_attempts import build_attempt_callback_id
from validibot.validations.services.execution_attempts import (
    build_callback_nonce_verifier,
)
from validibot.validations.tests.factories import ExecutionAttemptFactory


@override_settings(
    GCP_PROJECT_ID="validibot-project",
    WORKER_URL="https://worker.example",
)
class TestStorageCapabilityRefresh(TestCase):
    """Verify secret authentication and attempt lifecycle fencing on renewal."""

    def setUp(self):
        """Create one active GCS attempt with only a callback nonce verifier."""
        self.callback_nonce = "attempt-callback-secret"
        self.attempt = ExecutionAttemptFactory(
            state=ExecutionAttemptState.RUNNING,
            runner_type="google_cloud_run",
            execution_bundle_uri=("gs://validation/runs/org/run/attempts/attempt-1"),
            callback_nonce_hash=build_callback_nonce_verifier(self.callback_nonce),
        )
        self.payload = {
            "run_id": str(self.attempt.step_run.validation_run_id),
            "callback_id": build_attempt_callback_id(self.attempt),
            "callback_nonce": self.callback_nonce,
        }

    def _request(self, payload=None):
        """Build the minimal request shape consumed by the thin API view."""
        return SimpleNamespace(
            data=payload or self.payload,
        )

    @patch(
        "validibot.validations.api.storage_capabilities."
        "issue_attempt_gcs_runtime_capability"
    )
    def test_active_attempt_receives_no_store_prefix_identical_token(self, issue):
        """A valid active proof can renew authority without exposing it to caches."""
        issue.return_value = AttemptGCSRuntimeCapability(
            access_token="renewed-secret-token",  # noqa: S106 - test fixture
            expires_at=datetime.now(UTC) + timedelta(minutes=50),
            allowed_prefix=("gs://validation/runs/org/run/attempts/attempt-1/"),
            project_id="validibot-project",
            refresh_url=(
                "https://worker.example/api/v1/validation-storage-capabilities/refresh/"
            ),
        )

        response = ValidationStorageCapabilityRefreshView().post(self._request())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["access_token"], "renewed-secret-token")
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response["Pragma"], "no-cache")
        issue.assert_called_once_with(
            execution_bundle_uri=self.attempt.execution_bundle_uri,
            project_id="validibot-project",
            refresh_url=(
                "https://worker.example/api/v1/validation-storage-capabilities/refresh/"
            ),
        )

    @patch(
        "validibot.validations.api.storage_capabilities."
        "issue_attempt_gcs_runtime_capability"
    )
    def test_terminal_attempt_cannot_renew(self, issue):
        """Completion or cancellation fences new tokens before provider calls."""
        self.attempt.state = ExecutionAttemptState.COMPLETED
        self.attempt.save(update_fields=["state"])

        response = ValidationStorageCapabilityRefreshView().post(self._request())

        self.assertEqual(response.status_code, 409)
        issue.assert_not_called()

    @patch(
        "validibot.validations.api.storage_capabilities."
        "issue_attempt_gcs_runtime_capability"
    )
    def test_attempt_that_terminates_during_issuance_receives_no_token(self, issue):
        """A provider-call race cannot deliver newly minted terminal authority."""
        capability = AttemptGCSRuntimeCapability(
            access_token="discarded-secret-token",  # noqa: S106 - test fixture
            expires_at=datetime.now(UTC) + timedelta(minutes=50),
            allowed_prefix=("gs://validation/runs/org/run/attempts/attempt-1/"),
            project_id="validibot-project",
            refresh_url=(
                "https://worker.example/api/v1/validation-storage-capabilities/refresh/"
            ),
        )

        def _finish_attempt(**kwargs):
            """Simulate completion while the external token exchange is running."""
            self.attempt.state = ExecutionAttemptState.COMPLETED
            self.attempt.save(update_fields=["state"])
            return capability

        issue.side_effect = _finish_attempt

        response = ValidationStorageCapabilityRefreshView().post(self._request())

        self.assertEqual(response.status_code, 409)
        self.assertNotIn("access_token", response.data)

    @patch(
        "validibot.validations.api.storage_capabilities."
        "issue_attempt_gcs_runtime_capability"
    )
    def test_invalid_nonce_cannot_probe_or_renew_attempt(self, issue):
        """OIDC identity alone is insufficient without the attempt bearer proof."""
        payload = {**self.payload, "callback_nonce": "wrong-secret"}

        response = ValidationStorageCapabilityRefreshView().post(self._request(payload))

        self.assertEqual(response.status_code, 403)
        issue.assert_not_called()
