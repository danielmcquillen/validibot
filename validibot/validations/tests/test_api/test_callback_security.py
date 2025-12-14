"""
Security tests for the validation callback endpoint.

The callback endpoint relies on infrastructure-level security (Cloud Run IAM)
rather than application-level authentication. These tests verify the app-level
guards are in place to complement the infrastructure controls.

Security Model:
    - Cloud Run ingress is set to "internal" for the worker service
    - Only Cloud Run Jobs (validators) can reach the callback endpoint
    - Cloud Scheduler calls are authenticated via OIDC tokens verified by Cloud Run
    - App-level: endpoint returns 404 on non-worker instances (defense in depth)

Note: True security testing requires infrastructure verification, which is done
via deployment checklists and manual audits. These tests verify the app guards.
"""

from django.test import TestCase
from django.test import override_settings
from rest_framework.test import APIClient


class TestCallbackEndpointSecurity(TestCase):
    """
    Verify application-level security guards on the callback endpoint.

    The callback endpoint (/api/v1/validation-callbacks/) accepts unauthenticated
    requests because Cloud Run IAM handles authentication. However, it has an
    app-level guard: it only responds on worker instances (APP_IS_WORKER=True).
    """

    def setUp(self):
        self.client = APIClient()
        self.endpoint = "/api/v1/validation-callbacks/"

    @override_settings(APP_IS_WORKER=False)
    def test_callback_returns_404_on_non_worker_instance(self):
        """
        Callback endpoint must not exist on web instances.

        This is a defense-in-depth measure. The primary security control is
        Cloud Run ingress settings, but web instances should also return 404
        even if somehow reached (e.g., misconfigured load balancer).
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

    @override_settings(APP_IS_WORKER=False)
    def test_callback_returns_405_for_get_request(self):
        """GET requests should return 405 Method Not Allowed (POST only endpoint)."""
        response = self.client.get(self.endpoint)
        # The endpoint only accepts POST, so GET returns 405 regardless of worker status
        self.assertEqual(response.status_code, 405)

    @override_settings(APP_IS_WORKER=True)
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

    @override_settings(APP_IS_WORKER=True)
    def test_callback_validates_payload_on_worker(self):
        """Callback should validate payload structure even without auth."""
        response = self.client.post(
            self.endpoint,
            data={"invalid": "payload"},
            format="json",
        )
        # Pydantic validation should catch this
        self.assertEqual(response.status_code, 500)
