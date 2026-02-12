"""
Security tests for the validation callback endpoint.

Security is layered across multiple levels:
    1. URL routing: worker endpoints only exist on worker instances
    2. App guard: returns 404 on non-worker instances (defense in depth)
    3. API key: WORKER_API_KEY checked via WorkerKeyAuthentication (Docker Compose)
    4. Infrastructure: Cloud Run IAM / network isolation (GCP)

These tests verify the app-level guards (layers 1-2). Worker API key tests
are in core/tests/test_worker_auth.py.
"""

from django.test import TestCase
from django.test import override_settings
from rest_framework.test import APIClient


class TestCallbackEndpointSecurity(TestCase):
    """
    Verify application-level security guards on the callback endpoint.

    The callback endpoint (/api/v1/validation-callbacks/) accepts unauthenticated
    requests because infrastructure-level security handles authentication. It has
    an app-level guard: only responds on worker instances (APP_IS_WORKER=True).
    """

    def setUp(self):
        self.client = APIClient()
        self.endpoint = "/api/v1/validation-callbacks/"

    @override_settings(APP_IS_WORKER=False, ROOT_URLCONF="config.urls_web")
    def test_callback_returns_404_on_non_worker_instance(self):
        """
        Callback endpoint must not exist on web instances.

        This is a defense-in-depth measure. The primary security control is
        infrastructure-level (Cloud Run ingress, network isolation), but web
        instances should also return 404 even if somehow reached.

        Note: We must set ROOT_URLCONF explicitly because config/urls.py
        determines URL patterns at import time based on APP_IS_WORKER. Once
        imported, URL patterns don't change even with override_settings.
        """
        response = self.client.post(
            self.endpoint,
            data={
                "run_id": "00000000-0000-0000-0000-000000000000",
                "status": "success",
                "result_uri": "gs://fake/output.json",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    def test_callback_returns_405_for_get_request(self):
        """GET requests should return 405 Method Not Allowed (POST only endpoint)."""
        response = self.client.get(self.endpoint)
        # The endpoint only accepts POST, so GET returns 405
        self.assertEqual(response.status_code, 405)

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    def test_callback_accepts_post_on_worker_instance(self):
        """
        Callback endpoint should process requests on worker instances.

        Even with an invalid run_id, the endpoint should return a proper
        error response (404 for run not found) rather than a 404 for the
        endpoint itself.
        """
        response = self.client.post(
            self.endpoint,
            data={
                "run_id": "00000000-0000-0000-0000-000000000000",
                "callback_id": "test-callback-id",
                "status": "success",
                "result_uri": "gs://fake/output.json",
            },
            format="json",
        )
        # Should get 404 for "run not found", not for "endpoint not found"
        self.assertEqual(response.status_code, 404)
        self.assertIn("Validation run not found", response.json().get("error", ""))

    @override_settings(APP_IS_WORKER=True, ROOT_URLCONF="config.urls_worker")
    def test_callback_validates_payload_on_worker(self):
        """Callback should return 400 for invalid payloads."""
        response = self.client.post(
            self.endpoint,
            data={"invalid": "payload"},
            format="json",
        )
        # Pydantic validation should catch this and return 400 Bad Request
        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid callback payload", response.json().get("error", ""))
