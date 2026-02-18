"""
Authenticated smoke tests for post-deployment verification.

These tests log in as the superuser and verify that the full application
stack works end-to-end: sessions, database, dashboard, deep health checks,
and API authentication.

Skipped if SUPERUSER_USERNAME / SUPERUSER_PASSWORD are not in the environment.
"""

from __future__ import annotations

import re
from http import HTTPStatus

import pytest
import requests


@pytest.fixture(scope="module")
def logged_in_session(
    web_url: str,
    smoke_credentials: dict,
) -> requests.Session:
    """
    Create a requests session that is logged in as the superuser.

    Performs the full allauth login flow:
    1. GET the login page to obtain CSRF token
    2. POST credentials with CSRF token
    3. Verify redirect to dashboard
    """
    session = requests.Session()
    session.headers.update({"User-Agent": "validibot-smoke-tests/1.0"})

    # Step 1: GET login page for CSRF token
    login_url = f"{web_url}/accounts/login/"
    resp = session.get(login_url, timeout=30)
    assert resp.status_code == HTTPStatus.OK, f"Login page returned {resp.status_code}"

    # Extract CSRF token from form
    csrf_match = re.search(
        r'name="csrfmiddlewaretoken" value="([^"]+)"',
        resp.text,
    )
    assert csrf_match, "Could not find CSRF token on login page"
    csrf_token = csrf_match.group(1)

    # Step 2: POST login credentials
    resp = session.post(
        login_url,
        data={
            "csrfmiddlewaretoken": csrf_token,
            "login": smoke_credentials["username"],
            "password": smoke_credentials["password"],
        },
        headers={"Referer": login_url},
        timeout=30,
        allow_redirects=True,
    )

    # After successful login, allauth redirects to LOGIN_REDIRECT_URL
    assert resp.status_code == HTTPStatus.OK, (
        f"Post-login page returned {resp.status_code}, expected 200 after redirect"
    )

    # Verify we have a session cookie
    assert any("session" in c.name.lower() for c in session.cookies), (
        "No session cookie after login"
    )

    return session


class TestAuthentication:
    """Verify login and session management work correctly."""

    def test_login_succeeds(self, logged_in_session: requests.Session):
        """
        Superuser can log in via the allauth login form.

        This implicitly tests: database connectivity, session backend,
        CSRF protection, and the allauth authentication pipeline.
        """
        # The logged_in_session fixture already verifies login succeeded.
        # This test just makes the login step visible in test output.
        assert logged_in_session is not None


class TestAuthenticatedPages:
    """Verify authenticated pages are accessible after login."""

    def test_dashboard_accessible(
        self,
        web_url: str,
        logged_in_session: requests.Session,
    ):
        """
        The dashboard should be accessible with a valid session.

        Verifies the full request path: routing, middleware, auth,
        template rendering, and database queries.
        """
        resp = logged_in_session.get(
            f"{web_url}/app/dashboard/",
            timeout=30,
        )
        assert resp.status_code == HTTPStatus.OK, (
            f"Dashboard returned {resp.status_code}, expected 200"
        )

    def test_deep_health_check_passes(
        self,
        web_url: str,
        logged_in_session: requests.Session,
    ):
        """
        The deep health check should return healthy for a working deployment.

        Checks database, migrations, cache, storage, roles, validators,
        and security settings.
        """
        resp = logged_in_session.get(
            f"{web_url}/health/deep/",
            timeout=30,
        )
        assert resp.status_code == HTTPStatus.OK, (
            f"Deep health check returned {resp.status_code}, expected 200"
        )
        data = resp.json()
        assert data["status"] == "healthy", (
            f"Deep health check unhealthy: {data.get('checks', {})}"
        )

    def test_api_auth_me(
        self,
        web_url: str,
        logged_in_session: requests.Session,
    ):
        """
        The API /auth/me/ endpoint should return user data with a session.

        Verifies DRF session authentication works alongside allauth sessions.
        """
        resp = logged_in_session.get(
            f"{web_url}/api/v1/auth/me/",
            timeout=30,
            headers={"Accept": "application/json"},
        )
        assert resp.status_code == HTTPStatus.OK, (
            f"API auth/me returned {resp.status_code}, expected 200"
        )
        data = resp.json()
        assert "username" in data or "email" in data, (
            f"API auth/me response missing user data: {data}"
        )
