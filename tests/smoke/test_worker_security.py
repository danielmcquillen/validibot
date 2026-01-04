"""
Smoke tests for worker service security.

These tests verify that the worker service (which handles validator callbacks
and scheduled tasks) is properly protected by Cloud Run IAM.

Security Model:
    The worker service is deployed with --no-allow-unauthenticated, meaning
    Cloud Run IAM rejects requests that don't have a valid identity token.
    This is the PRIMARY security control for internal endpoints.

    Application-level guards (APP_IS_WORKER check) provide defense in depth
    but are secondary to the infrastructure-level IAM protection.
"""

from __future__ import annotations

import requests


class TestWorkerServiceIAM:
    """Verify the worker service rejects unauthenticated requests."""

    def test_unauthenticated_request_rejected(
        self,
        worker_url: str,
        http_session: requests.Session,
    ):
        """
        Unauthenticated requests to the worker service should be rejected.

        Cloud Run IAM should return 403 Forbidden before the request reaches
        Django. This is the primary security control.
        """
        response = http_session.get(worker_url, timeout=30)

        # Cloud Run returns 403 for unauthenticated requests to IAM-protected services
        assert response.status_code == 403, (
            f"Worker service returned {response.status_code}, expected 403 Forbidden. "
            "This suggests the worker service may not be properly protected by IAM. "
            "Check that it was deployed with --no-allow-unauthenticated."
        )

    def test_callback_endpoint_rejects_unauthenticated(
        self,
        worker_url: str,
        http_session: requests.Session,
    ):
        """
        The callback endpoint should reject unauthenticated POST requests.

        This is the critical security test - validator callbacks must not be
        spoofable by external attackers.
        """
        response = http_session.post(
            f"{worker_url}/api/v1/validation-callbacks/",
            json={
                "run_id": "00000000-0000-0000-0000-000000000000",
                "callback_id": "test-callback",
                "status": "success",
                "result_uri": "gs://fake-bucket/fake-path/output.json",
            },
            timeout=30,
        )

        # Should get 403 from Cloud Run IAM, not 200/404/500 from Django
        assert response.status_code == 403, (
            f"Callback endpoint returned {response.status_code}, expected 403 Forbidden. "
            "CRITICAL: If this returns 200, 404, or 500, the callback endpoint may be "
            "exposed without IAM protection. This is a security vulnerability."
        )

    def test_scheduled_task_endpoints_reject_unauthenticated(
        self,
        worker_url: str,
        http_session: requests.Session,
    ):
        """
        Scheduled task endpoints should reject unauthenticated requests.

        These endpoints are called by Cloud Scheduler with OIDC tokens.
        External requests should be rejected by IAM.
        """
        scheduled_endpoints = [
            "/api/v1/scheduled/cleanup-idempotency-keys/",
            "/api/v1/scheduled/cleanup-callback-receipts/",
            "/api/v1/scheduled/clear-sessions/",
            "/api/v1/scheduled/purge-expired-submissions/",
            "/api/v1/scheduled/cleanup-stuck-runs/",
        ]

        for endpoint in scheduled_endpoints:
            response = http_session.post(
                f"{worker_url}{endpoint}",
                json={},
                timeout=30,
            )
            assert response.status_code == 403, (
                f"Scheduled endpoint {endpoint} returned {response.status_code}, "
                "expected 403. This endpoint may be exposed without IAM protection."
            )


class TestWorkerServiceAuthenticated:
    """
    Verify the worker service works with authenticated requests.

    These tests use the authenticated session (with gcloud identity token)
    to verify that legitimate requests are processed correctly.
    """

    def test_authenticated_request_reaches_django(
        self,
        worker_url: str,
        authenticated_http_session: requests.Session,
    ):
        """
        Authenticated requests should pass IAM and reach Django.

        We expect a 404 or 405 (since there's no root handler), not 403.
        This proves IAM is accepting authenticated requests.
        """
        response = authenticated_http_session.get(worker_url, timeout=30)

        # Should NOT be 403 - IAM should accept the request
        assert response.status_code != 403, (
            "Authenticated request was rejected by IAM. Check that your gcloud "
            "credentials have the 'Cloud Run Invoker' role on the worker service."
        )

        # We expect 404 (no root handler) or 200 (if there's a catch-all)
        assert response.status_code in (200, 404, 405), (
            f"Unexpected status code {response.status_code} for authenticated request"
        )

    def test_callback_endpoint_authenticated_reaches_django(
        self,
        worker_url: str,
        authenticated_http_session: requests.Session,
    ):
        """
        Authenticated callback requests should reach Django and be processed.

        With a fake run_id, we expect a 404 "Validation run not found" response
        from Django, not a 403 from IAM. This proves the request reached the app.
        """
        response = authenticated_http_session.post(
            f"{worker_url}/api/v1/validation-callbacks/",
            json={
                "run_id": "00000000-0000-0000-0000-000000000000",
                "callback_id": "smoke-test-callback",
                "status": "success",
                "result_uri": "gs://fake-bucket/fake-path/output.json",
            },
            timeout=30,
        )

        # Should NOT be 403 - request should reach Django
        assert response.status_code != 403, (
            "Authenticated callback request was rejected by IAM."
        )

        # Expect 404 "Validation run not found" since we used a fake UUID
        assert response.status_code == 404, (
            f"Expected 404 (run not found), got {response.status_code}. "
            f"Response: {response.text[:200]}"
        )

        # Verify the error message is from Django, not Cloud Run
        data = response.json()
        assert "run" in data.get("error", "").lower(), (
            f"Expected 'run not found' error from Django, got: {data}"
        )
