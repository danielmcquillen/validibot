"""
Tests for worker API key authentication.

The WorkerKeyAuthentication class provides shared-secret authentication for
internal worker endpoints. It protects against SSRF attacks in Docker Compose
deployments where all containers share the same Docker network.

When WORKER_API_KEY is configured, callers must include it in the
Authorization header. When not configured, the check is skipped (for GCP
deployments where Cloud Run IAM handles authentication).
"""

from django.test import TestCase
from django.test import override_settings
from rest_framework.test import APIClient


class TestWorkerKeyAuthentication(TestCase):
    """Verify the WorkerKeyAuthentication class enforces API key checks."""

    def setUp(self):
        self.client = APIClient()
        self.callback_endpoint = "/api/v1/validation-callbacks/"
        self.scheduled_endpoint = "/api/v1/scheduled/clear-sessions/"

    # -------------------------------------------------------------------------
    # Key not configured (GCP path) - should allow requests without a key
    # -------------------------------------------------------------------------

    @override_settings(
        APP_IS_WORKER=True,
        ROOT_URLCONF="config.urls_worker",
        WORKER_API_KEY="",
    )
    def test_no_key_configured_allows_request(self):
        """When WORKER_API_KEY is not set, requests are allowed without a key."""
        response = self.client.post(
            self.callback_endpoint,
            data={
                "run_id": "00000000-0000-0000-0000-000000000000",
                "callback_id": "test-callback-id",
                "status": "success",
                "result_uri": "gs://fake/output.json",
            },
            format="json",
        )
        # Should reach Django (404 for run not found, not 403 for auth failure)
        self.assertEqual(response.status_code, 404)
        self.assertIn("Validation run not found", response.json().get("error", ""))

    # -------------------------------------------------------------------------
    # Key configured - should reject requests without a key
    # -------------------------------------------------------------------------

    @override_settings(
        APP_IS_WORKER=True,
        ROOT_URLCONF="config.urls_worker",
        WORKER_API_KEY="test-secret-key-12345",
    )
    def test_key_configured_rejects_request_without_header(self):
        """When WORKER_API_KEY is set, requests without Authorization are rejected."""
        response = self.client.post(
            self.callback_endpoint,
            data={
                "run_id": "00000000-0000-0000-0000-000000000000",
                "status": "success",
                "result_uri": "gs://fake/output.json",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(
        APP_IS_WORKER=True,
        ROOT_URLCONF="config.urls_worker",
        WORKER_API_KEY="test-secret-key-12345",
    )
    def test_key_configured_rejects_wrong_key(self):
        """When WORKER_API_KEY is set, requests with the wrong key are rejected."""
        self.client.credentials(HTTP_AUTHORIZATION="Worker-Key wrong-key")
        response = self.client.post(
            self.callback_endpoint,
            data={
                "run_id": "00000000-0000-0000-0000-000000000000",
                "status": "success",
                "result_uri": "gs://fake/output.json",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(
        APP_IS_WORKER=True,
        ROOT_URLCONF="config.urls_worker",
        WORKER_API_KEY="test-secret-key-12345",
    )
    def test_key_configured_rejects_wrong_scheme(self):
        """Authorization header with wrong scheme (Bearer instead of Worker-Key)."""
        self.client.credentials(
            HTTP_AUTHORIZATION="Bearer test-secret-key-12345",
        )
        response = self.client.post(
            self.callback_endpoint,
            data={
                "run_id": "00000000-0000-0000-0000-000000000000",
                "status": "success",
                "result_uri": "gs://fake/output.json",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    # -------------------------------------------------------------------------
    # Key configured - should accept requests with correct key
    # -------------------------------------------------------------------------

    @override_settings(
        APP_IS_WORKER=True,
        ROOT_URLCONF="config.urls_worker",
        WORKER_API_KEY="test-secret-key-12345",
    )
    def test_key_configured_accepts_correct_key(self):
        """When WORKER_API_KEY is set, requests with the correct key are accepted."""
        self.client.credentials(
            HTTP_AUTHORIZATION="Worker-Key test-secret-key-12345",
        )
        response = self.client.post(
            self.callback_endpoint,
            data={
                "run_id": "00000000-0000-0000-0000-000000000000",
                "callback_id": "test-callback-id",
                "status": "success",
                "result_uri": "gs://fake/output.json",
            },
            format="json",
        )
        # Should reach Django (404 for run not found, not 403 for auth)
        self.assertEqual(response.status_code, 404)
        self.assertIn("Validation run not found", response.json().get("error", ""))

    # -------------------------------------------------------------------------
    # Scheduled task endpoints use the same auth
    # -------------------------------------------------------------------------

    @override_settings(
        APP_IS_WORKER=True,
        ROOT_URLCONF="config.urls_worker",
        WORKER_API_KEY="test-secret-key-12345",
    )
    def test_scheduled_endpoint_rejects_without_key(self):
        """Scheduled task endpoints also require the worker API key."""
        response = self.client.post(
            self.scheduled_endpoint,
            data={},
            format="json",
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(
        APP_IS_WORKER=True,
        ROOT_URLCONF="config.urls_worker",
        WORKER_API_KEY="test-secret-key-12345",
    )
    def test_scheduled_endpoint_accepts_correct_key(self):
        """Scheduled task endpoints accept requests with the correct key."""
        self.client.credentials(
            HTTP_AUTHORIZATION="Worker-Key test-secret-key-12345",
        )
        response = self.client.post(
            self.scheduled_endpoint,
            data={},
            format="json",
        )
        # Should reach Django and process (200 for success)
        self.assertEqual(response.status_code, 200)
