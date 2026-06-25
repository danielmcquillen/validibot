"""
Authentication: extract credentials and claims from FastMCP's request context.

The MCP server is authenticated-only. The user's agent sends either an
MCP-scoped OAuth access token or a legacy Validibot API token in the
``Authorization: Bearer <token>`` header. FastMCP's auth middleware validates it
at the HTTP layer (before the stale-request bug #1233 can bite), and we read the
result via ``get_access_token()``.
"""

from __future__ import annotations

import logging

from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token

from validibot_mcp.errors import MCPToolError

logger = logging.getLogger(__name__)


class AuthenticationError(MCPToolError):
    """Raised when the MCP request is missing or has an invalid auth header."""

    default_code = "UNAUTHORIZED"


# ── Bearer token (authenticated path) ──────────────────────────────


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

    Returns:
        The raw Bearer credential string, or None if no Bearer token was
        provided.
    """
    access_token = get_access_token()
    if access_token is not None and access_token.token:
        return access_token.token
    return None


def get_access_token_claims() -> dict[str, object]:
    """Return JWT claims from FastMCP's validated bearer credential."""

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
