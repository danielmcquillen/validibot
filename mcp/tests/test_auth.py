"""
Tests for Bearer token extraction from FastMCP's auth context.

The MCP server authenticates each tool call by reading the Bearer token
from FastMCP's ``get_access_token()`` context. These tests verify that
valid tokens are extracted correctly and that missing or empty tokens
produce clear ``AuthenticationError`` exceptions.

They also cover the security boundary between ``/mcp`` and ``/public-mcp``:
that decision must use the raw ASGI path, not Starlette's reconstructed
``request.url.path``, because malformed Host headers used to poison that
property in Starlette before CVE-2026-48710 was fixed.

The ``mock_access_token`` fixture mocks ``get_access_token()`` to return
a fake ``AccessToken`` with a configurable ``.token`` value.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from validibot_mcp.auth import AuthenticationError, get_api_key, get_api_key_or_none


def _request_with_paths(*, scope_path: str, url_path: str) -> SimpleNamespace:
    """Build the minimum request shape needed to test path-source handling."""

    return SimpleNamespace(
        scope={"path": scope_path},
        url=SimpleNamespace(path=url_path),
    )


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

    def test_public_surface_decision_uses_raw_asgi_path(self, monkeypatch):
        """Public MCP requests must ignore bearer headers based on the routed path.

        Starlette's reconstructed ``request.url.path`` is not the routing
        source of truth. If it disagrees with ``scope["path"]``, the MCP auth
        boundary should follow the raw ASGI path and stay anonymous.
        """

        monkeypatch.setattr(
            "validibot_mcp.auth.get_http_request",
            lambda: _request_with_paths(scope_path="/public-mcp", url_path="/mcp"),
        )
        monkeypatch.setattr(
            "validibot_mcp.auth.get_access_token",
            lambda: pytest.fail("public surface must not read bearer credentials"),
        )

        assert get_api_key_or_none() is None

    def test_poisoned_url_path_cannot_mark_authenticated_surface_public(self, monkeypatch):
        """A Host-poisoned URL path must not bypass the authenticated surface.

        This models CVE-2026-48710: routing saw ``/mcp`` but
        ``request.url.path`` was reconstructed as ``/public-mcp``. The MCP
        server must keep treating the request as authenticated.
        """

        token = MagicMock()
        token.token = "my-secret-key"
        monkeypatch.setattr(
            "validibot_mcp.auth.get_http_request",
            lambda: _request_with_paths(scope_path="/mcp", url_path="/public-mcp"),
        )
        monkeypatch.setattr("validibot_mcp.auth.get_access_token", lambda: token)

        assert get_api_key_or_none() == "my-secret-key"
