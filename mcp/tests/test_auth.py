"""
Tests for Bearer token extraction from FastMCP's auth context.

The MCP server is authenticated-only: it authenticates each tool call by
reading the Bearer token from FastMCP's ``get_access_token()`` context. These
tests verify that valid tokens are extracted correctly, that missing or empty
tokens produce clear ``AuthenticationError`` exceptions, and that
``get_api_key_or_none`` reports presence/absence for the (now single)
authenticated surface.

The ``mock_access_token`` fixture mocks ``get_access_token()`` to return
a fake ``AccessToken`` with a configurable ``.token`` value.
"""

from __future__ import annotations

import pytest

from validibot_mcp.auth import AuthenticationError, get_api_key, get_api_key_or_none


class TestGetApiKey:
    """Extract Bearer tokens from FastMCP's auth context."""

    def test_valid_bearer_token(self, mock_access_token):
        """A valid AccessToken with a non-empty token should return the token."""
        mock_access_token("my-secret-key")
        assert get_api_key() == "my-secret-key"

    def test_none_access_token(self, mock_access_token):
        """When get_access_token() returns None, raise AuthenticationError.

        This happens when no Authorization header was sent, or when the
        auth provider rejected the token.
        """
        mock_access_token(None)
        with pytest.raises(AuthenticationError, match="Missing or invalid"):
            get_api_key()

    def test_empty_token_string(self, mock_access_token):
        """An AccessToken with an empty token string should raise AuthenticationError.

        An empty token is not useful — reject it early rather than letting
        it hit the REST API as an invalid credential.
        """
        mock_access_token("")
        with pytest.raises(AuthenticationError, match="Missing or invalid"):
            get_api_key()

    def test_api_key_or_none_returns_token_when_present(self, mock_access_token):
        """A present bearer credential should be returned by the optional getter.

        WHY: tool handlers call ``get_api_key`` (which raises) for the required
        path, but ``get_api_key_or_none`` underpins it and must faithfully
        surface a valid token when one is on the request.
        """
        mock_access_token("my-secret-key")
        assert get_api_key_or_none() == "my-secret-key"

    def test_api_key_or_none_returns_none_when_absent(self, mock_access_token):
        """No bearer credential should yield ``None`` from the optional getter.

        WHY: this is the signal the required ``get_api_key`` turns into an
        ``AuthenticationError`` — there is no anonymous fallback surface, so an
        absent token must read as ``None`` rather than an empty string.
        """
        mock_access_token(None)
        assert get_api_key_or_none() is None
