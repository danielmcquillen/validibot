"""Legacy FastMCP TokenVerifier for Validibot API tokens.

The MCP server now prefers MCP-scoped OAuth access tokens validated locally
via JWKS. This verifier remains as a compatibility path for manual
``mcp-remote`` setups that still pass a legacy Validibot API token in the
``Authorization`` header.

It validates each Bearer token by calling the Validibot REST API's
``GET /api/v1/auth/me/`` endpoint, which returns 200 with user info if the
token is valid, or 403 if not.

Why a custom TokenVerifier instead of DebugTokenVerifier?

    ``DebugTokenVerifier`` is explicitly documented as bypassing standard
    security checks and intended for development/testing only. In production,
    using a class called "Debug" is a code smell — even if the downstream API
    does the real auth.

    A custom verifier gives us:
    - **Fail-fast** — invalid tokens are rejected at the MCP layer before
      any tool code runs, saving wasted REST API calls.
    - **Caching** — valid tokens are cached (default 5 min) so the verification
      call only happens once per token per TTL window, not on every tool call.
    - **Production-appropriate naming** — no "Debug" in production code.
    - **Proper logging** — we can see which tokens are valid/invalid.

Why keep this verifier at all?

    Manual bearer-token setups still exist in our desired UX. Those tokens are
    opaque DRF tokens, not JWTs, so they need a fallback verifier.

Why not IntrospectionTokenVerifier (RFC 7662)?

    The Validibot REST API uses DRF's ``TokenAuthentication`` with opaque API
    keys. The ``/api/v1/auth/me/`` endpoint returns ``{"email": ..., "name": ...}``,
    not the RFC 7662 ``{"active": true, ...}`` format. Adding a new introspection
    endpoint just for MCP would be unnecessary coupling. This custom verifier
    calls the existing endpoint and interprets 200 as "active".
"""

from __future__ import annotations

import logging
import time
from threading import Lock

import httpx
from fastmcp.server.auth.auth import AccessToken, TokenVerifier

logger = logging.getLogger(__name__)


class ValidibotTokenVerifier(TokenVerifier):
    """Validate legacy Validibot API tokens by calling the REST API.

    On each new token, makes a ``GET /api/v1/auth/me/`` request with the
    Bearer token. A 200 response means the token is valid; anything else
    means it's not.

    Valid tokens are cached in-memory for ``cache_ttl_seconds`` to avoid
    redundant network calls. The cache is bounded by ``max_cache_size``
    entries.

    Args:
        api_base_url: Base URL of the Validibot REST API
            (e.g. ``https://api.validibot.com``).
        cache_ttl_seconds: How long to cache valid tokens (default: 300 = 5 min).
            Set to 0 to disable caching.
        max_cache_size: Maximum cached tokens (default: 1000). When exceeded,
            the oldest entries are evicted.
        http_client: Optional ``httpx.AsyncClient`` for connection reuse.
            If not provided, a new client is created per verification call.
    """

    _AUTH_ME_PATH = "/api/v1/auth/me/"

    def __init__(
        self,
        *,
        api_base_url: str,
        scopes: list[str] | None = None,
        cache_ttl_seconds: int = 300,
        max_cache_size: int = 1000,
        http_client: httpx.AsyncClient | None = None,
    ):
        super().__init__()
        self._api_base_url = api_base_url.rstrip("/")
        self._scopes = list(scopes or [])
        self._cache_ttl = cache_ttl_seconds
        self._max_cache_size = max_cache_size
        self._http_client = http_client

        # Cache: token → (AccessToken, expiry_time)
        self._cache: dict[str, tuple[AccessToken, float]] = {}
        self._cache_lock = Lock()

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify a Bearer token against the Validibot REST API.

        Returns an ``AccessToken`` if valid, ``None`` if not. The token
        string is preserved in the ``AccessToken.token`` field so that
        downstream tool code can forward it to the REST API via
        ``get_access_token().token``.
        """
        # Check cache first
        cached = self._get_cached(token)
        if cached is not None:
            return cached

        # Call the REST API to validate
        access_token = await self._verify_against_api(token)

        if access_token is not None and self._cache_ttl > 0:
            self._set_cached(token, access_token)

        return access_token

    async def _verify_against_api(self, token: str) -> AccessToken | None:
        """Make the actual HTTP call to verify the token."""
        url = f"{self._api_base_url}{self._AUTH_ME_PATH}"
        try:
            if self._http_client is not None:
                response = await self._http_client.get(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                )
            else:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.get(
                        url,
                        headers={"Authorization": f"Bearer {token}"},
                    )

            if response.status_code == 200:
                data = response.json()
                logger.debug("Token verified for user: %s", data.get("email", "unknown"))
                return AccessToken(
                    token=token,
                    client_id=data.get("email", "unknown"),
                    scopes=list(self._scopes),
                )

            logger.debug("Token verification failed: HTTP %d", response.status_code)
            return None

        except httpx.HTTPError:
            logger.exception("Token verification request failed")
            return None

    # ── Cache management ──────────────────────────────────────────────

    def _get_cached(self, token: str) -> AccessToken | None:
        """Return cached AccessToken if still valid, None otherwise."""
        with self._cache_lock:
            entry = self._cache.get(token)
            if entry is None:
                return None

            access_token, expiry = entry
            if time.monotonic() > expiry:
                del self._cache[token]
                return None

            return access_token

    def _set_cached(self, token: str, access_token: AccessToken) -> None:
        """Cache a verified token. Evicts oldest entries if at capacity.
        No-op when caching is disabled (cache_ttl_seconds=0).
        """
        if self._cache_ttl <= 0:
            return

        with self._cache_lock:
            # Simple eviction: if at max size, remove the oldest entry
            if len(self._cache) >= self._max_cache_size:
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]

            self._cache[token] = (access_token, time.monotonic() + self._cache_ttl)
