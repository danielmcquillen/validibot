"""FastMCP server setup and ASGI app.

This module exposes two MCP surfaces backed by the same tool implementations:

- ``/mcp``: authenticated surface for OAuth and manual bearer-token access.
- ``/public-mcp``: anonymous surface for public x402 workflows only.

Both surfaces run inside one ASGI application so Cloud Run can serve them from
the same container while keeping their auth policies separate.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastmcp import FastMCP
from fastmcp.server.auth import MultiAuth
from fastmcp.server.auth.oidc_proxy import OIDCProxy
from fastmcp.utilities.logging import configure_logging as _configure_fastmcp_logging
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from validibot_mcp.client import aclose_http_clients
from validibot_mcp.config import get_settings
from validibot_mcp.license_check import verify_license_or_die
from validibot_mcp.token_verifier import ValidibotTokenVerifier
from validibot_mcp.x402 import aclose_x402_http_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_settings = get_settings()


# Configure logging level from env var (VALIDIBOT_LOG_LEVEL).
# Set to DEBUG to see JWT claim mismatches during auth troubleshooting.
# Uses FastMCP's own configure_logging so the RichHandler and propagation
# settings are respected.
_configure_fastmcp_logging(level=_settings.log_level.upper())  # type: ignore[arg-type]
logging.basicConfig(
    level=getattr(logging, _settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

_logger = logging.getLogger(__name__)
# Build a startup-info dict from explicitly-non-sensitive Settings fields
# only (URLs, scopes, log level). Never include client_secret, signing
# keys, or anything else that could be marked sensitive in Pydantic
# Field metadata. CodeQL's clear-text-logging query taints any
# attribute access on a Settings instance because the class also holds
# oauth_client_secret; constructing the dict explicitly makes the
# safety auditable at a glance.
_startup_log_fields = {
    "log_level": _settings.log_level,
    "jwks_uri": _settings.effective_oauth_jwks_url,
    "issuer": _settings.oauth_authorization_server_url.rstrip("/"),
    "audience": _settings.effective_oauth_resource_audience,
}
_logger.info("MCP server starting. %s", _startup_log_fields)

_AUTHENTICATED_INSTRUCTIONS = (
    "Validibot validates building energy models and technical data "
    "against configurable workflows.\n\n"
    "## Authenticated surface\n\n"
    "Use this endpoint when you connected with OAuth or a manual bearer API "
    "token.\n\n"
    "1. **Discover workflows** with `list_workflows`.\n"
    "   - Results include `workflow_ref`, `org_slug`, and access metadata.\n"
    "   - Discovery returns workflows across all organizations you can access "
    "plus public x402 workflows.\n"
    "2. **Inspect a workflow** with `get_workflow_details(workflow_ref=...)`.\n"
    "3. **Submit a file** with `validate_file(workflow_ref=..., ...)`.\n"
    "   - Member-access workflows use your authenticated quota.\n"
    "   - Public x402-only workflows still use the payment-backed path.\n"
    "4. **Check results** with `get_run_status(run_ref=...)` or "
    "`wait_for_run(run_ref=...)`.\n"
    "   - `run_ref` hides org and wallet polling details."
)

_PUBLIC_INSTRUCTIONS = (
    "Validibot validates building energy models and technical data "
    "against configurable workflows.\n\n"
    "## Public anonymous surface\n\n"
    "Use this endpoint when you do not have a Validibot account.\n\n"
    "1. **Discover workflows** with `list_workflows`.\n"
    "   - This surface returns only public x402 workflows.\n"
    "2. **Inspect a workflow** with `get_workflow_details(workflow_ref=...)`.\n"
    "3. **Submit a file** with `validate_file(workflow_ref=..., ...)`.\n"
    "   - Anonymous validation requires a `PAYMENT-SIGNATURE` header.\n"
    "4. **Check results** with `get_run_status(run_ref=...)` or "
    "`wait_for_run(run_ref=...)`."
)


def _register_toolset(server: FastMCP) -> None:
    """Register the shared Validibot tool implementations on ``server``."""

    from validibot_mcp.tools.runs import register_tools as register_run_tools
    from validibot_mcp.tools.validate import register_tools as register_validate_tools
    from validibot_mcp.tools.workflows import register_tools as register_workflow_tools

    register_workflow_tools(server)
    register_validate_tools(server)
    register_run_tools(server)


# ── Auth provider ──────────────────────────────────────────────────────
#
# Claude Desktop (as of April 2026) ignores authorization_endpoint and
# token_endpoint from OAuth metadata and instead constructs them from the
# MCP server's base URL.  This means RemoteAuthProvider (which advertises
# the external Django auth server) does not work — Claude sends the token
# exchange to the MCP server, which has no /token endpoint.
#
# The workaround is OIDCProxy: it proxies /authorize and /token requests
# from the MCP server to the upstream Django OIDC provider, so Claude's
# hardcoded URL construction lands on working endpoints.
#
# See: https://github.com/anthropics/claude-ai-mcp/issues/82
#
# The MCP server registers as a *confidential* client with the Django
# OIDC provider (separate from the public Claude Desktop client).  Claude
# Desktop does Dynamic Client Registration with the MCP server's OAuthProxy,
# and the MCP server uses its own credentials to exchange codes upstream.
#
# Legacy API tokens are still supported via MultiAuth fallback.

_legacy_api_token_verifier = ValidibotTokenVerifier(
    api_base_url=_settings.api_base_url,
    scopes=_settings.effective_oauth_required_scopes,
)

_auth_provider: OIDCProxy | MultiAuth

if _settings.oauth_client_secret:
    _auth_provider = OIDCProxy(
        config_url=f"{_settings.oauth_authorization_server_url.rstrip('/')}/.well-known/openid-configuration",
        client_id=_settings.oauth_client_id,
        client_secret=_settings.oauth_client_secret,
        base_url=_settings.mcp_base_url.rstrip("/"),
        required_scopes=_settings.effective_oauth_required_scopes,
        require_authorization_consent=False,
        token_endpoint_auth_method="client_secret_post",  # noqa: S106
    )
    _logger.info(
        "Auth: OIDCProxy (client_id=%s, upstream=%s)",
        _settings.oauth_client_id,
        _settings.oauth_authorization_server_url,
    )
else:
    # Fallback for environments without OAuth configured (local dev).
    # Legacy API token verifier only.
    _auth_provider = MultiAuth(
        verifiers=[_legacy_api_token_verifier],
    )
    _logger.info("Auth: legacy API token only (no OAuth client configured)")

authenticated_mcp = FastMCP(
    name="Validibot",
    instructions=_AUTHENTICATED_INSTRUCTIONS,
    auth=_auth_provider,
)
public_mcp = FastMCP(
    name="Validibot Public",
    instructions=_PUBLIC_INSTRUCTIONS,
)

_register_toolset(authenticated_mcp)
_register_toolset(public_mcp)

_authenticated_http_app = authenticated_mcp.http_app(path="/mcp")
_public_http_app = public_mcp.http_app(path="/public-mcp")


async def root(request: Request) -> JSONResponse:
    """Service info describing both MCP connection surfaces."""

    return JSONResponse(
        {
            "service": "validibot-mcp",
            "protocol": "Model Context Protocol (MCP)",
            "mcp_endpoint": "/mcp",
            "public_mcp_endpoint": "/public-mcp",
            "docs": "https://docs.validibot.com/integrations/mcp/",
        }
    )


@asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncIterator[None]:
    """Start and stop the authenticated and public MCP sub-app lifespans.

    The license gate runs first. It raises ``LicenseCheckError`` and
    aborts startup if the community deployment does not advertise the
    ``mcp_server`` feature — i.e. if validibot-pro/enterprise is not
    installed on the API the MCP server fronts. See ``license_check``.
    """

    await verify_license_or_die()
    async with _authenticated_http_app.router.lifespan_context(_authenticated_http_app):
        async with _public_http_app.router.lifespan_context(_public_http_app):
            try:
                yield
            finally:
                await aclose_http_clients()
                await aclose_x402_http_client()


app = Starlette(
    routes=[
        Route("/", endpoint=root, methods=["GET"]),
        *_authenticated_http_app.routes,
        *_public_http_app.routes,
    ],
    lifespan=_lifespan,
    middleware=_authenticated_http_app.user_middleware,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("validibot_mcp.server:app", host="0.0.0.0", port=8080)  # noqa: S104
