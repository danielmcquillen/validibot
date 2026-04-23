"""
Authentication: extract credentials and claims from FastMCP's request context.

The MCP server supports two authentication paths:

1. **Authenticated path** (Bearer token): The user's agent sends either an
   MCP-scoped OAuth access token or a legacy Validibot API token in the
   ``Authorization: Bearer <token>`` header. FastMCP's auth middleware
   validates it. We read the result via ``get_access_token()``.

2. **Anonymous path** (Payment signature): The agent sends a
   ``PAYMENT-SIGNATURE`` header containing an x402 v2 payment payload.
   No Bearer token is present. We read this via ``get_http_request()``.

Why two different FastMCP mechanisms?
    ``get_access_token()`` works for Bearer tokens because FastMCP has built-in
    auth middleware that extracts and validates them at the HTTP layer, before
    the stale-request bug (#1233) can bite.

    ``get_http_request()`` is needed for ``PAYMENT-SIGNATURE`` because
    FastMCP has no built-in x402 support. This function IS affected by
    bug #1233 (it can return a stale request from the session-init call).
    We add defensive null checks and log warnings when the header is
    missing unexpectedly.
"""

from __future__ import annotations

import logging

from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token, get_http_request

from validibot_mcp.errors import MCPToolError

logger = logging.getLogger(__name__)
_PUBLIC_MCP_PATH_PREFIX = "/public-mcp"


class AuthenticationError(MCPToolError):
    """Raised when the MCP request is missing or has an invalid auth header."""

    default_code = "UNAUTHORIZED"


# ── Bearer token (authenticated path) ──────────────────────────────


def _is_public_surface_request() -> bool:
    """Return True when the current request targets the anonymous MCP surface.

    FastMCP still exposes any incoming ``Authorization`` header through
    ``get_access_token()`` even when the transport has no auth provider. The
    public ``/public-mcp`` surface must ignore those headers so the tool layer
    stays anonymous and continues down the x402 path.
    """

    try:
        request = get_http_request()
    except RuntimeError:
        return False
    if request is None:
        return False
    return request.url.path.startswith(_PUBLIC_MCP_PATH_PREFIX)


def get_api_key() -> str:
    """Extract Bearer credential, raising if absent.

    Use this in code paths that REQUIRE an authenticated user (e.g.
    the authenticated path in tool handlers).

    Returns:
        The raw Bearer credential string (OAuth token or legacy API token).

    Raises:
        AuthenticationError: If no valid Bearer token was provided.
    """
    token = get_api_key_or_none()
    if token is not None:
        return token
    msg = "Missing or invalid Authorization header. Provide a Bearer token."
    raise AuthenticationError(msg)


def get_api_key_or_none() -> str | None:
    """Extract Bearer credential, returning None if absent.

    Use this for two-path dispatch: the return value tells you which
    path the request is on.

    Returns:
        The raw Bearer credential string, or None if no Bearer token was
        provided.
    """
    if _is_public_surface_request():
        return None

    access_token = get_access_token()
    if access_token is not None and access_token.token:
        return access_token.token
    return None


def get_access_token_claims() -> dict[str, object]:
    """Return JWT claims from FastMCP's validated bearer credential."""

    if _is_public_surface_request():
        return {}

    access_token = get_access_token()
    if access_token is None:
        return {}
    token_with_claims = access_token if isinstance(access_token, AccessToken) else None
    if token_with_claims is None or not token_with_claims.claims:
        return {}
    return dict(token_with_claims.claims)


def get_authenticated_user_sub_or_none() -> str | None:
    """Return the OIDC subject when the current bearer token is OAuth."""

    user_sub = str(get_access_token_claims().get("sub", "")).strip()
    return user_sub or None


# ── x402 payment signature (anonymous path) ────────────────────────


def get_payment_signature() -> str | None:
    """Extract the PAYMENT-SIGNATURE header from the HTTP request.

    This is the x402 v2 header that agents send after constructing a
    USDC payment on Base. The value is a base64-encoded signed payment
    payload that the Coinbase facilitator can verify.

    Uses ``get_http_request()`` which is affected by FastMCP bug #1233
    (stale request context). We add defensive checks and log a warning
    if the request is None unexpectedly — this helps diagnose #1233
    occurrences in production.

    Returns:
        The raw PAYMENT-SIGNATURE header value, or None if not present.
    """
    try:
        request = get_http_request()
    except RuntimeError:
        # No active HTTP request context (e.g. in tests or non-HTTP transports).
        return None
    if request is None:
        logger.warning(
            "get_http_request() returned None — possible FastMCP #1233 "
            "stale-request bug. PAYMENT-SIGNATURE extraction failed.",
        )
        return None
    return request.headers.get("payment-signature")


def get_stripe_spt() -> str | None:
    """Extract the Stripe Shared Payment Token from the request headers.

    Retained for backward compatibility with the ACP billing mode.
    Not used on the anonymous x402 path.

    Returns:
        The raw SPT string, or None if the header is not present.
    """
    try:
        request = get_http_request()
    except RuntimeError:
        return None
    if request is None:
        return None
    return request.headers.get("x-stripe-spt")
