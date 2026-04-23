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
    VALIDIBOT_X402_ENABLED: x402 payment kill switch (default False).
        When False, anonymous agents get PAYMENT_REQUIRED but the MCP
        server does not attempt facilitator verification.
    VALIDIBOT_X402_TEST_MODE: When True, all x402 configuration resolves
        from the ``VALIDIBOT_TEST_X402_*`` variables instead of the
        production ``VALIDIBOT_X402_*`` ones.  This mirrors the Stripe
        test/live key pattern — both sets live in the same env file and
        a single flag switches between them.
    VALIDIBOT_X402_PAY_TO_ADDRESS: CDP wallet address that receives USDC
        (production — Base mainnet).
    VALIDIBOT_X402_NETWORK: CAIP-2 network identifier (default Base mainnet).
    VALIDIBOT_X402_ASSET: USDC contract address on the target network
        (default Base mainnet USDC).
    VALIDIBOT_X402_FACILITATOR_URL: Coinbase CDP facilitator endpoint
        (default production).
    VALIDIBOT_TEST_X402_PAY_TO_ADDRESS: Testnet receiving wallet (Base Sepolia).
    VALIDIBOT_TEST_X402_NETWORK: Testnet CAIP-2 network identifier
        (default Base Sepolia).
    VALIDIBOT_TEST_X402_ASSET: Testnet USDC contract address
        (default USDC on Base Sepolia).
    VALIDIBOT_TEST_X402_FACILITATOR_URL: Testnet facilitator endpoint
        (default x402.org open facilitator).
    VALIDIBOT_AGENT_API_BASE_URL: Base URL for the agent API endpoints.
        Defaults to the same as API_BASE_URL. Separable for local dev
        where the MCP server and Django may run on different ports.
    VALIDIBOT_MCP_SERVICE_KEY: Shared secret for MCP→Django service auth
        in local development (bypasses Cloud Run OIDC).
    VALIDIBOT_MCP_SERVICE_AUDIENCE: Optional explicit audience used when the
        MCP Cloud Run service fetches an identity token for the Django helper
        endpoints. Defaults to API_BASE_URL.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """MCP server configuration — no database, no Django."""

    # Log level for the MCP server and FastMCP auth internals.
    # Set to DEBUG to see JWT claim mismatches during auth troubleshooting.
    log_level: str = "INFO"

    # ── Core ────────────────────────────────────────────────────────
    # Public base URL of this MCP service. Used for RFC 9728 protected
    # resource metadata and as the default OAuth audience root.
    mcp_base_url: str = "https://mcp.validibot.com"

    # Upstream authorization server that issues OAuth access tokens for the
    # authenticated MCP surface.
    oauth_authorization_server_url: str = "https://app.validibot.com"

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

    # The Validibot REST API that this MCP server proxies.
    # Also used as the default audience for Cloud Run identity tokens
    # (via effective_mcp_service_audience) — must match Django's
    # MCP_OIDC_AUDIENCE setting.
    api_base_url: str = "https://app.validibot.com"

    # Global kill switch. When False, the server returns 503 on every tool
    # call. Checked on each request — no restart needed to take effect.
    mcp_enabled: bool = True

    # ── Agent API ───────────────────────────────────────────────────
    # Base URL for agent-specific endpoints (POST /api/v1/agent/runs/, etc.)
    # Defaults to api_base_url. Separable for local dev where the MCP
    # server and Django may be on different hosts/ports.
    agent_api_base_url: str = ""

    # Shared secret for MCP→Django service-to-service auth in local dev.
    # In production, Cloud Run OIDC tokens are used instead.
    mcp_service_key: str = ""

    # Explicit service-to-service audience override. When blank, the MCP
    # server targets the primary API base URL with its Cloud Run identity token.
    mcp_service_audience: str = ""

    # ── x402 Payments ───────────────────────────────────────────────
    # Second kill switch — allows disabling x402 payment processing
    # without disabling the entire MCP server. When False, anonymous
    # agents receive PAYMENT_REQUIRED but the server does not call
    # the facilitator.
    x402_enabled: bool = False

    # When True, x402_network / x402_asset / x402_pay_to_address /
    # x402_facilitator_url resolve from the TEST_X402_* env vars
    # instead of the production X402_* ones.  Both sets can coexist
    # in the same env file — flip this flag to switch.
    x402_test_mode: bool = False

    # ── x402 production values (Base mainnet) ──────────────────────
    # Read from VALIDIBOT_X402_* (same env var names as before).
    # Access via the x402_network / x402_asset / etc. properties
    # below — they respect x402_test_mode.

    # CDP wallet address that receives USDC payments (0x...).
    live_x402_pay_to_address: str = Field(
        default="",
        validation_alias="VALIDIBOT_X402_PAY_TO_ADDRESS",
    )

    # CAIP-2 network identifier. Base mainnet = "eip155:8453".
    live_x402_network: str = Field(
        default="eip155:8453",
        validation_alias="VALIDIBOT_X402_NETWORK",
    )

    # USDC contract address — Base mainnet USDC.
    live_x402_asset: str = Field(
        default="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        validation_alias="VALIDIBOT_X402_ASSET",
    )

    # Coinbase CDP facilitator endpoint (production).
    live_x402_facilitator_url: str = Field(
        default="https://api.cdp.coinbase.com/platform/v2/x402",
        validation_alias="VALIDIBOT_X402_FACILITATOR_URL",
    )

    # ── x402 test values (Base Sepolia) ────────────────────────────
    # Read from VALIDIBOT_TEST_X402_*.  Defaults are sensible for
    # Base Sepolia testnet so you only need to set
    # TEST_X402_PAY_TO_ADDRESS for a working test setup.

    # Testnet receiving wallet (Base Sepolia).
    test_x402_pay_to_address: str = ""

    # Base Sepolia testnet.
    test_x402_network: str = "eip155:84532"

    # USDC on Base Sepolia.
    test_x402_asset: str = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"

    # x402.org open testnet facilitator (no API credentials required).
    test_x402_facilitator_url: str = "https://x402.org/facilitator"

    model_config = {
        "env_prefix": "VALIDIBOT_",
        # Cloud Run mounts secrets from Secret Manager as a file at
        # /secrets/.env.  Pydantic-settings reads this automatically
        # when env_file is set.  OS env vars take precedence over the
        # file, so --set-env-vars in the deploy recipe still wins.
        "env_file": "/secrets/.env",
        "env_file_encoding": "utf-8",
    }

    # ── x402 resolved properties ─────────────────────────────────────
    # The rest of the codebase reads these properties. They delegate
    # to the test or live backing fields based on x402_test_mode.

    @property
    def x402_network(self) -> str:
        """CAIP-2 network — testnet or mainnet based on x402_test_mode."""
        if self.x402_test_mode:
            return self.test_x402_network
        return self.live_x402_network

    @property
    def x402_asset(self) -> str:
        """USDC contract address — testnet or mainnet based on x402_test_mode."""
        if self.x402_test_mode:
            return self.test_x402_asset
        return self.live_x402_asset

    @property
    def x402_pay_to_address(self) -> str:
        """Receiving wallet — testnet or mainnet based on x402_test_mode."""
        if self.x402_test_mode:
            return self.test_x402_pay_to_address
        return self.live_x402_pay_to_address

    @property
    def x402_facilitator_url(self) -> str:
        """Facilitator endpoint — testnet or mainnet based on x402_test_mode."""
        if self.x402_test_mode:
            return self.test_x402_facilitator_url
        return self.live_x402_facilitator_url

    # ── Other resolved properties ─────────────────────────────────

    @property
    def effective_agent_api_base_url(self) -> str:
        """Agent API URL, falling back to the main API URL."""
        return self.agent_api_base_url or self.api_base_url

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
