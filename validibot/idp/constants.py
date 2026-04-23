"""Constants shared by the cloud OIDC provider and client bootstrap.

These values define the default Claude Desktop / Claude Code client
registration for the cloud-only MCP OAuth flow. The management command and
settings layer both import from here so the canonical client shape lives in
one place.
"""

from __future__ import annotations

CLAUDE_OIDC_CLIENT_ID = "validibot-claude-desktop"
CLAUDE_OIDC_CLIENT_NAME = "Claude Desktop"
CLAUDE_OIDC_SCOPES = (
    "openid",
    "profile",
    "email",
    "validibot:mcp",
)
CLAUDE_OIDC_GRANT_TYPES = (
    "authorization_code",
    "refresh_token",
)
CLAUDE_OIDC_RESPONSE_TYPES = ("code",)
OIDC_TOKEN_ENDPOINT_AUTH_METHODS = (
    "none",
    "client_secret_post",
)
OIDC_CODE_CHALLENGE_METHODS = ("S256",)
CLAUDE_OIDC_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)


# ── MCP server confidential client ────────────────────────────────────
# The MCP Cloud Run service registers as a confidential OAuth client so
# it can proxy the OAuth flow on behalf of MCP clients like Claude Desktop.
# This is required because Claude Desktop ignores external authorization
# endpoints and constructs auth URLs from the MCP server's base URL.
# See: https://github.com/anthropics/claude-ai-mcp/issues/82

MCP_SERVER_OIDC_CLIENT_ID = "validibot-mcp-server"
MCP_SERVER_OIDC_CLIENT_NAME = "Validibot MCP Server"
MCP_SERVER_OIDC_SCOPES = (
    "openid",
    "profile",
    "email",
    "validibot:mcp",
)
MCP_SERVER_OIDC_GRANT_TYPES = (
    "authorization_code",
    "refresh_token",
)
MCP_SERVER_OIDC_RESPONSE_TYPES = ("code",)
MCP_SERVER_OIDC_REDIRECT_URIS = ("https://mcp.validibot.com/auth/callback",)


def normalize_oidc_values(values: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Return a stable deduplicated tuple of non-empty OIDC config values."""

    seen: set[str] = set()
    normalized: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        normalized.append(cleaned)
        seen.add(cleaned)
    return tuple(normalized)
