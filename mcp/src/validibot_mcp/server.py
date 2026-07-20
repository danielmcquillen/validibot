"""FastMCP server setup and ASGI app.

This module exposes a single authenticated MCP surface:

- ``/mcp``: authenticated surface for OAuth 2.1 and manual bearer-token access.

It runs inside one ASGI application so Cloud Run can serve it from a single
container.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastmcp import FastMCP
from fastmcp.server.auth import MultiAuth
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration, OIDCProxy
from fastmcp.utilities.logging import configure_logging as _configure_fastmcp_logging
from pydantic import AnyHttpUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from validibot_mcp.client import aclose_http_clients
from validibot_mcp.config import Settings, get_settings
from validibot_mcp.license_check import verify_license_or_die
from validibot_mcp.token_verifier import ValidibotTokenVerifier

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
_logger.info("MCP server starting")

_AUTHENTICATED_INSTRUCTIONS = (
    "Validibot validates building energy models and technical data "
    "against configurable workflows.\n\n"
    "## Authenticated surface\n\n"
    "Use this endpoint with OAuth or a manual bearer API token.\n\n"
    "1. **Discover workflows** with `list_workflows`.\n"
    "   - Results include `workflow_ref`, `org_slug`, and access metadata.\n"
    "   - Discovery returns workflows across all organizations you can access.\n"
    "2. **Inspect a workflow** with `get_workflow_details(workflow_ref=...)`.\n"
    "3. **Submit a file** with `validate_file(workflow_ref=..., ...)`.\n"
    "   - Runs use your authenticated quota.\n"
    "4. **Check results** with `get_run_status(run_ref=...)` or "
    "`wait_for_run(run_ref=...)`.\n"
    "   - `run_ref` hides org polling details."
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


class ConfiguredOIDCProxy(OIDCProxy):
    """OIDC proxy backed by Validibot's locally configured provider metadata.

    FastMCP normally downloads the upstream discovery document in
    ``OIDCProxy.__init__``. That makes container readiness depend on Django
    being publicly reachable, which is deliberately false during a
    maintenance-safe deployment. Validibot owns both sides of this contract,
    so the proxy uses the same stable endpoint paths that Django publishes.
    Runtime authorization and token exchange still go to Django normally.
    """

    def __init__(
        self,
        *,
        oidc_configuration: OIDCConfiguration,
        **kwargs: object,
    ) -> None:
        """Initialize the proxy without an eager discovery-network request."""
        self._configured_oidc_configuration = oidc_configuration
        super().__init__(**kwargs)  # type: ignore[arg-type]

    def get_oidc_configuration(
        self,
        config_url: AnyHttpUrl,
        strict: bool | None,
        timeout_seconds: int | None,
    ) -> OIDCConfiguration:
        """Return trusted local metadata instead of fetching ``config_url``."""
        del config_url, strict, timeout_seconds
        return self._configured_oidc_configuration


def _build_oidc_proxy(settings: Settings) -> OIDCProxy:
    """Build the OIDCProxy auth provider with RFC 8707 audience binding.

    WHY: The default ``JWTVerifier`` inside ``OIDCProxy`` only validates the
    access token's ``aud`` claim when an ``audience`` is supplied. Without it,
    a token minted for *any* resource served by the same authorization server
    would be accepted here — defeating RFC 8707 resource indicators. We pass
    ``effective_oauth_resource_audience`` so the verifier rejects tokens whose
    ``aud`` is not this MCP surface, and ``resource_base_url`` so the advertised
    RFC 9728 protected-resource metadata names the same audience the verifier
    enforces.

    Args:
        settings: The resolved MCP server settings carrying the OAuth client
            credentials and the effective resource audience.

    Returns:
        An ``OIDCProxy`` whose JWTVerifier enforces the ``aud`` claim.
    """

    issuer = settings.oauth_authorization_server_url.rstrip("/")
    oidc_configuration = OIDCConfiguration(
        issuer=issuer,
        authorization_endpoint=settings.effective_oauth_authorization_endpoint,
        token_endpoint=settings.effective_oauth_token_endpoint,
        revocation_endpoint=settings.effective_oauth_revocation_endpoint,
        jwks_uri=settings.effective_oauth_jwks_url,
        response_types_supported=["code"],
        subject_types_supported=["public"],
        id_token_signing_alg_values_supported=["RS256"],
        token_endpoint_auth_methods_supported=["client_secret_post"],
    )

    return ConfiguredOIDCProxy(
        oidc_configuration=oidc_configuration,
        # FastMCP still requires a valid URL here, but ConfiguredOIDCProxy
        # supplies the configuration locally and never requests this URL.
        config_url=f"{settings.oauth_authorization_server_url.rstrip('/')}/.well-known/openid-configuration",
        client_id=settings.oauth_client_id,
        client_secret=settings.oauth_client_secret,
        base_url=settings.mcp_base_url.rstrip("/"),
        audience=settings.effective_oauth_resource_audience,
        resource_base_url=settings.effective_oauth_resource_audience,
        required_scopes=settings.effective_oauth_required_scopes,
        require_authorization_consent=False,
        token_endpoint_auth_method="client_secret_post",  # noqa: S106
    )


_auth_provider: OIDCProxy | MultiAuth

if _settings.oauth_client_secret:
    _auth_provider = _build_oidc_proxy(_settings)
    _logger.info("Auth: OIDCProxy enabled")
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

_register_toolset(authenticated_mcp)

_authenticated_http_app = authenticated_mcp.http_app(path="/mcp")


async def root(request: Request) -> JSONResponse:
    """Service descriptor for the MCP connection surface.

    A human/developer-facing index — NOT a discovery document any MCP client
    depends on. MCP clients use the endpoint + RFC 9728 protected resource
    metadata. We point at those real mechanisms and summarise the surface.
    """

    settings = get_settings()
    base = settings.mcp_base_url.rstrip("/")
    descriptor: dict[str, object] = {
        "service": "validibot-mcp",
        "protocol": "Model Context Protocol (MCP)",
        "mcp_protocol_version": "2025-06-18",
        "surfaces": {
            "authenticated": {
                "endpoint": f"{base}/mcp",
                "auth": "OAuth 2.1 (Dynamic Client Registration) or legacy bearer token",
                "oauth_metadata": f"{base}/.well-known/oauth-protected-resource/mcp",
            },
        },
        "docs": "https://docs.validibot.com/api/mcp-integration/",
    }
    return JSONResponse(descriptor)


@asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncIterator[None]:
    """Start and stop the authenticated MCP sub-app lifespan.

    The license gate runs first when MCP is enabled. It raises
    ``LicenseCheckError`` and aborts startup if the community deployment does
    not advertise the ``mcp_server`` feature — i.e. if
    validibot-pro/enterprise is not installed on the API the MCP server
    fronts. Maintenance deployments explicitly disable MCP, allowing a
    zero-capacity internal revision to become ready while Django is offline.
    The gate runs again when maintenance is removed and MCP is re-enabled.
    """

    if _settings.mcp_enabled:
        await verify_license_or_die()
    else:
        _logger.info("MCP disabled; deferring the license check until it is enabled")
    async with _authenticated_http_app.router.lifespan_context(_authenticated_http_app):
        try:
            yield
        finally:
            await aclose_http_clients()


app = Starlette(
    routes=[
        Route("/", endpoint=root, methods=["GET"]),
        *_authenticated_http_app.routes,
    ],
    lifespan=_lifespan,
    middleware=_authenticated_http_app.user_middleware,
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("validibot_mcp.server:app", host="0.0.0.0", port=8080)  # noqa: S104
