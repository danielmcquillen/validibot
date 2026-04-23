"""Unit tests for the legacy Validibot API-token verifier.

The ``ValidibotTokenVerifier`` validates Bearer tokens by calling the
Validibot REST API's ``/api/v1/auth/me/`` endpoint. These tests verify:

1. **Valid tokens** — a 200 response from ``/auth/me/`` returns an
   ``AccessToken`` with the original token preserved for forwarding.
2. **Invalid tokens** — non-200 responses (403, 500, etc.) return None.
3. **Network errors** — connection failures are handled gracefully.
4. **Caching** — valid tokens are cached to avoid redundant API calls,
   and cache expiry works correctly.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import respx
from httpx import Response

from validibot_mcp.token_verifier import ValidibotTokenVerifier

API_BASE = "https://api.validibot.com"
AUTH_ME_URL = f"{API_BASE}/api/v1/auth/me/"
TEST_TOKEN = "test-api-key-abc123"


# ── Token validation ─────────────────────────────────────────────────
# Tests for the core verify_token() method, verifying that valid and
# invalid tokens are handled correctly against the REST API.


class TestVerifyToken:
    """Verify token validation against the REST API."""

    @respx.mock
    async def test_valid_token_returns_access_token(self):
        """A 200 response from /auth/me/ should return an AccessToken
        with the original token preserved for downstream forwarding.
        """
        respx.get(AUTH_ME_URL).mock(
            return_value=Response(200, json={"email": "user@example.com", "name": "Test User"}),
        )
        verifier = ValidibotTokenVerifier(api_base_url=API_BASE, cache_ttl_seconds=0)

        result = await verifier.verify_token(TEST_TOKEN)

        assert result is not None
        assert result.token == TEST_TOKEN
        assert result.client_id == "user@example.com"

    @respx.mock
    async def test_invalid_token_returns_none(self):
        """A 403 response from /auth/me/ means the token is invalid."""
        respx.get(AUTH_ME_URL).mock(
            return_value=Response(403, json={"detail": "Invalid token."}),
        )
        verifier = ValidibotTokenVerifier(api_base_url=API_BASE, cache_ttl_seconds=0)

        result = await verifier.verify_token(TEST_TOKEN)

        assert result is None

    @respx.mock
    async def test_server_error_returns_none(self):
        """A 500 response should be treated as an invalid token (fail safe)."""
        respx.get(AUTH_ME_URL).mock(
            return_value=Response(500, text="Internal Server Error"),
        )
        verifier = ValidibotTokenVerifier(api_base_url=API_BASE, cache_ttl_seconds=0)

        result = await verifier.verify_token(TEST_TOKEN)

        assert result is None

    @respx.mock
    async def test_network_error_returns_none(self):
        """Network failures should return None, not raise exceptions."""
        import httpx

        respx.get(AUTH_ME_URL).mock(side_effect=httpx.ConnectError("Connection refused"))
        verifier = ValidibotTokenVerifier(api_base_url=API_BASE, cache_ttl_seconds=0)

        result = await verifier.verify_token(TEST_TOKEN)

        assert result is None

    @respx.mock
    async def test_sends_bearer_header(self):
        """The token should be sent as a Bearer header to /auth/me/."""
        route = respx.get(AUTH_ME_URL).mock(
            return_value=Response(200, json={"email": "user@example.com", "name": "Test"}),
        )
        verifier = ValidibotTokenVerifier(api_base_url=API_BASE, cache_ttl_seconds=0)

        await verifier.verify_token(TEST_TOKEN)

        assert route.called
        auth_header = route.calls[0].request.headers["Authorization"]
        assert auth_header == f"Bearer {TEST_TOKEN}"


# ── Caching ──────────────────────────────────────────────────────────
# The verifier caches valid tokens to avoid redundant API calls on every
# MCP request. These tests verify cache behavior.


class TestCaching:
    """Verify token caching reduces redundant API calls."""

    @respx.mock
    async def test_cached_token_avoids_api_call(self):
        """A previously verified token should be served from cache without
        hitting the REST API again.
        """
        route = respx.get(AUTH_ME_URL).mock(
            return_value=Response(200, json={"email": "user@example.com", "name": "Test"}),
        )
        verifier = ValidibotTokenVerifier(api_base_url=API_BASE, cache_ttl_seconds=300)

        # First call — hits the API
        result1 = await verifier.verify_token(TEST_TOKEN)
        assert result1 is not None
        assert route.call_count == 1

        # Second call — served from cache
        result2 = await verifier.verify_token(TEST_TOKEN)
        assert result2 is not None
        assert result2.token == TEST_TOKEN
        assert route.call_count == 1  # no additional API call

    @respx.mock
    async def test_expired_cache_hits_api_again(self):
        """After cache TTL expires, the next verification should call the API."""
        route = respx.get(AUTH_ME_URL).mock(
            return_value=Response(200, json={"email": "user@example.com", "name": "Test"}),
        )
        verifier = ValidibotTokenVerifier(api_base_url=API_BASE, cache_ttl_seconds=1)

        # First call
        await verifier.verify_token(TEST_TOKEN)
        assert route.call_count == 1

        # Simulate cache expiry by advancing the monotonic clock
        with patch("validibot_mcp.token_verifier.time") as mock_time:
            mock_time.monotonic.return_value = time.monotonic() + 2
            await verifier.verify_token(TEST_TOKEN)

        assert route.call_count == 2

    async def test_cache_disabled_when_ttl_zero(self):
        """With cache_ttl_seconds=0, no caching should occur."""
        verifier = ValidibotTokenVerifier(api_base_url=API_BASE, cache_ttl_seconds=0)

        # Pre-verify that the cache is empty
        assert len(verifier._cache) == 0

        # Even after a hypothetical set, nothing should be cached
        from fastmcp.server.auth.auth import AccessToken

        verifier._set_cached("token", AccessToken(token="token", client_id="test", scopes=[]))
        assert len(verifier._cache) == 0
