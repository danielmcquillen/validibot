"""
Smoke tests for the web service.

These tests verify that the public-facing web service is accessible and
responding correctly after deployment.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import requests


class TestWebServiceHealth:
    """Verify the web service is accessible and healthy."""

    def test_homepage_returns_200(self, web_url: str, http_session: requests.Session):
        """
        The homepage should be accessible without authentication.

        This is a basic smoke test to verify the web service is running.
        """
        response = http_session.get(web_url, timeout=30)
        assert response.status_code == HTTPStatus.OK, (
            f"Homepage returned {response.status_code}, expected 200"
        )

    def test_robots_txt_accessible(
        self,
        web_url: str,
        http_session: requests.Session,
    ):
        """
        robots.txt should be accessible.

        This verifies static file serving and basic routing work.
        """
        response = http_session.get(f"{web_url}/robots.txt", timeout=30)
        assert response.status_code == HTTPStatus.OK, (
            f"robots.txt returned {response.status_code}, expected 200"
        )

    def test_api_docs_accessible(self, web_url: str, http_session: requests.Session):
        """
        API documentation should be accessible.

        This verifies DRF Spectacular is configured correctly.
        """
        response = http_session.get(f"{web_url}/api/v1/docs/", timeout=30)
        assert response.status_code == HTTPStatus.OK, (
            f"API docs returned {response.status_code}, expected 200"
        )

    def test_api_schema_accessible(self, web_url: str, http_session: requests.Session):
        """
        OpenAPI schema should be accessible.
        """
        response = http_session.get(f"{web_url}/api/v1/schema/", timeout=30)
        assert response.status_code == HTTPStatus.OK, (
            f"API schema returned {response.status_code}, expected 200"
        )


class TestWebServiceAPI:
    """Verify the public API endpoints are working."""

    def test_auth_me_requires_authentication(
        self,
        web_url: str,
        http_session: requests.Session,
    ):
        """
        The /api/v1/auth/me/ endpoint should require authentication.

        Unauthenticated requests should get 401 Unauthorized.
        """
        response = http_session.get(f"{web_url}/api/v1/auth/me/", timeout=30)
        assert response.status_code == HTTPStatus.UNAUTHORIZED, (
            f"auth/me returned {response.status_code}, expected 401 Unauthorized"
        )

    def test_workflows_list_requires_authentication(
        self,
        web_url: str,
        http_session: requests.Session,
    ):
        """
        The workflows API should require authentication for listing.
        """
        response = http_session.get(f"{web_url}/api/v1/workflows/", timeout=30)
        assert response.status_code == HTTPStatus.UNAUTHORIZED, (
            f"workflows list returned {response.status_code}, expected 401"
        )
