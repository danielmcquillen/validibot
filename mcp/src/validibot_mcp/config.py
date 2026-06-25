"""
Application configuration loaded from environment variables.

The MCP server has no Django dependency and no database connection.
All configuration comes from environment variables with the ``VALIDIBOT_``
prefix, parsed by pydantic-settings.

Environment variables:
    VALIDIBOT_MCP_BASE_URL: Public base URL of the MCP service.
    VALIDIBOT_OAUTH_AUTHORIZATION_SERVER_URL: Base URL of the upstream
        authorization server that issues MCP OAuth tokens.
    VALIDIBOT_OAUTH_JWKS_URL: Optional JWKS URL override for JWT validation.
        Defaults to ``<authorization_server>/.well-known/jwks.json``.
    VALIDIBOT_OAUTH_RESOURCE_AUDIENCE: Audience that must appear on MCP OAuth
        access tokens. Defaults to ``<mcp_base_url>/mcp``.
    VALIDIBOT_OAUTH_REQUIRED_SCOPE: Scope that must appear on MCP OAuth
        access tokens. Defaults to ``validibot:mcp``.
    VALIDIBOT_API_BASE_URL: Base URL of the Validibot REST API.
    VALIDIBOT_MCP_ENABLED: Global kill switch (default True). Set to False
        to return 503 on all tool calls without redeploying.
    VALIDIBOT_MCP_SERVICE_KEY: Shared secret for MCP→Django service auth
        in local development (bypasses Cloud Run OIDC).
    VALIDIBOT_MCP_SERVICE_AUDIENCE: Optional explicit audience used when the
        MCP Cloud Run service fetches an identity token for the Django helper
        endpoints. Defaults to API_BASE_URL.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """MCP server configuration — no database, no Django."""

    # Log level for the MCP server and FastMCP auth internals.
    # Set to DEBUG to see JWT claim mismatches during auth troubleshooting.
    log_level: str = "INFO"

    # ── Core ────────────────────────────────────────────────────────
    # Public base URL of this MCP service. Used for RFC 9728 protected
    # resource metadata and as the OAuth audience root. REQUIRED — there is
    # deliberately NO default: this community package must not hardcode a
    # hosted (validibot.com) URL, so every deployment sets
    # VALIDIBOT_MCP_BASE_URL explicitly. Local/self-hosted deployments read it
    # from .mcp; GCP stamps it from .build. Settings construction raises if it
    # is missing.
    mcp_base_url: str

    # Upstream authorization server that issues OAuth access tokens for the
    # authenticated MCP surface. REQUIRED — set
    # VALIDIBOT_OAUTH_AUTHORIZATION_SERVER_URL in .mcp; no hosted default.
    oauth_authorization_server_url: str

    # Confidential OAuth client credentials registered with the upstream
    # OIDC provider.  The MCP server uses these to exchange authorization
    # codes for tokens on behalf of MCP clients (OIDCProxy pattern).
    # When oauth_client_secret is empty, OAuth is disabled and only the
    # legacy API token path is available.
    oauth_client_id: str = "validibot-mcp-server"
    oauth_client_secret: str = ""

    # Optional JWKS URL override. When blank, derived from the authorization
    # server's standard allauth OIDC JWKS endpoint.
    oauth_jwks_url: str = ""

    # Optional explicit audience override. When blank, defaults to the
    # canonical protected resource URL for this MCP surface.
    oauth_resource_audience: str = ""

    # Single required scope for the authenticated MCP surface.
    oauth_required_scope: str = "validibot:mcp"

    # The Validibot REST API that this MCP server proxies. Also the default
    # audience for Cloud Run identity tokens (via effective_mcp_service_audience).
    # On GCP the deploy recipes stamp .build's VALIDIBOT_MCP_API_BASE_URL onto
    # MCP as this value and onto Django as MCP_OIDC_AUDIENCE. Local/self-hosted
    # deployments set VALIDIBOT_API_BASE_URL in .mcp.
    api_base_url: str

    # Global kill switch. When False, the server returns 503 on every tool
    # call. Checked on each request — no restart needed to take effect.
    mcp_enabled: bool = True

    # ── Service-to-service auth ─────────────────────────────────────
    # Shared secret for MCP→Django service-to-service auth in local dev.
    # In production, Cloud Run OIDC tokens are used instead.
    mcp_service_key: str = ""

    # Explicit service-to-service audience override. When blank, the MCP
    # server targets the primary API base URL with its Cloud Run identity token.
    mcp_service_audience: str = ""

    model_config = {
        "env_prefix": "VALIDIBOT_",
        # Cloud Run mounts secrets from Secret Manager as a file at
        # /secrets/.env.  Pydantic-settings reads this automatically
        # when env_file is set.  OS env vars take precedence over the
        # file, so --set-env-vars in the deploy recipe still wins.
        "env_file": "/secrets/.env",
        "env_file_encoding": "utf-8",
    }

    # ── Resolved properties ───────────────────────────────────────

    @property
    def effective_mcp_service_audience(self) -> str:
        """Return the audience used for MCP→Django identity tokens."""

        return self.mcp_service_audience or self.api_base_url

    @property
    def effective_oauth_jwks_url(self) -> str:
        """Return the JWT verification key endpoint for MCP OAuth tokens."""
        if self.oauth_jwks_url:
            return self.oauth_jwks_url
        return f"{self.oauth_authorization_server_url.rstrip('/')}/.well-known/jwks.json"

    @property
    def effective_oauth_resource_audience(self) -> str:
        """Return the audience value required on MCP OAuth access tokens."""
        if self.oauth_resource_audience:
            return self.oauth_resource_audience
        return f"{self.mcp_base_url.rstrip('/')}/mcp"

    @property
    def effective_oauth_required_scopes(self) -> list[str]:
        """Return the non-empty required OAuth scopes for the MCP surface."""
        scope = self.oauth_required_scope.strip()
        return [scope] if scope else []


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings singleton."""
    return Settings()
