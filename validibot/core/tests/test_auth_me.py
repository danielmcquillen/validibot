"""Tests for the auth/me API endpoint."""

import pytest
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from validibot.users.tests.factories import UserFactory


@pytest.fixture
def api_client() -> APIClient:
    return APIClient()


@pytest.fixture
def user(db):
    """Create a test user with email and name."""
    return UserFactory(
        email="test@example.com",
        name="Test User",
    )


@pytest.fixture
def auth_token(user):
    """Create an auth token for the test user."""
    token, _ = Token.objects.get_or_create(user=user)
    return token


class TestAuthMeEndpoint:
    """Tests for GET /api/v1/auth/me/ endpoint."""

    def test_returns_user_info_for_authenticated_request(
        self,
        api_client: APIClient,
        user,
        auth_token,
    ):
        """Test that authenticated users receive their email and name."""
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth_token.key}")

        response = api_client.get("/api/v1/auth/me/")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["email"] == "test@example.com"
        assert response.data["name"] == "Test User"

    def test_returns_empty_string_for_blank_name(
        self,
        api_client: APIClient,
        db,
    ):
        """Test that users without a name get an empty string."""
        user = UserFactory(email="noname@example.com", name="")
        token, _ = Token.objects.get_or_create(user=user)
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {token.key}")

        response = api_client.get("/api/v1/auth/me/")

        assert response.status_code == status.HTTP_200_OK
        assert response.data["email"] == "noname@example.com"
        assert response.data["name"] == ""

    def test_rejects_unauthenticated_request(
        self,
        api_client: APIClient,
    ):
        """Test that unauthenticated requests are rejected."""
        response = api_client.get("/api/v1/auth/me/")

        # DRF returns 403 Forbidden when no valid authentication is provided
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_rejects_invalid_token(
        self,
        api_client: APIClient,
        db,
    ):
        """Test that invalid tokens are rejected."""
        api_client.credentials(HTTP_AUTHORIZATION="Bearer invalid-token-12345")

        response = api_client.get("/api/v1/auth/me/")

        # DRF returns 403 Forbidden when token authentication fails
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_response_only_contains_email_and_name(
        self,
        api_client: APIClient,
        user,
        auth_token,
    ):
        """Test that response contains only the expected fields (minimal exposure)."""
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {auth_token.key}")

        response = api_client.get("/api/v1/auth/me/")

        assert response.status_code == status.HTTP_200_OK
        assert set(response.data.keys()) == {"email", "name"}
