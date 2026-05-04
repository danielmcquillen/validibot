"""Async HTTP clients for the Validibot MCP service.

The MCP server has no direct database access, so every workflow lookup and run
operation goes through the cloud Django API. This module owns the shared
``httpx.AsyncClient`` instances used for:

1. **Authenticated MCP helper endpoints** (``/api/v1/mcp/...``), where the
   MCP service forwards an already-validated user identity over a trusted
   service-to-service channel.
2. **Anonymous agent endpoints** (``/api/v1/agent/...``), where the MCP
   service forwards x402 payment metadata for public workflows.
3. **Cloud Run metadata server calls**, used to mint the MCP service's OIDC
   identity token in production.

The clients are lazily initialized and closed by the ASGI lifespan so the
process reuses connection pools without leaking sockets in tests or on
shutdown.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from http import HTTPStatus
from typing import Any

import httpx

from validibot_mcp.config import get_settings
from validibot_mcp.errors import MCPToolError

logger = logging.getLogger(__name__)

settings = get_settings()
_METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)
_SERVICE_TOKEN_CACHE_TTL_SECONDS = 300

_http: httpx.AsyncClient | None = None
_metadata_http: httpx.AsyncClient | None = None
_agent_http: httpx.AsyncClient | None = None
_http_client_lock = asyncio.Lock()
_service_identity_lock = asyncio.Lock()
_service_identity_cache: tuple[str, float] | None = None


def _build_api_http_client() -> httpx.AsyncClient:
    """Create the shared Django API client for MCP helper calls."""

    return httpx.AsyncClient(
        base_url=settings.api_base_url,
        timeout=httpx.Timeout(30.0, connect=5.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    )


def _build_metadata_http_client() -> httpx.AsyncClient:
    """Create the client used for Cloud Run metadata server calls."""

    return httpx.AsyncClient(
        timeout=httpx.Timeout(5.0, connect=2.0),
        headers={"Metadata-Flavor": "Google"},
        trust_env=False,
    )


def _build_agent_http_client() -> httpx.AsyncClient:
    """Create the shared client for anonymous agent endpoints."""

    return httpx.AsyncClient(
        base_url=settings.effective_agent_api_base_url,
        timeout=httpx.Timeout(30.0, connect=5.0),
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
    )


async def _get_http_client() -> httpx.AsyncClient:
    """Return the shared Django API client, initializing it once."""

    global _http

    if _http is not None:
        return _http

    async with _http_client_lock:
        if _http is None:
            _http = _build_api_http_client()
    return _http


async def _get_metadata_http_client() -> httpx.AsyncClient:
    """Return the shared metadata-server client, initializing it once."""

    global _metadata_http

    if _metadata_http is not None:
        return _metadata_http

    async with _http_client_lock:
        if _metadata_http is None:
            _metadata_http = _build_metadata_http_client()
    return _metadata_http


async def _get_agent_http() -> httpx.AsyncClient:
    """Return the shared client for anonymous agent endpoints."""

    global _agent_http

    if _agent_http is not None:
        return _agent_http

    async with _http_client_lock:
        if _agent_http is None:
            _agent_http = _build_agent_http_client()
    return _agent_http


async def aclose_http_clients() -> None:
    """Close all lazily initialized MCP HTTP clients.

    The ASGI lifespan calls this during shutdown so the process does not leak
    connection pools across test runs or worker restarts.
    """

    global _http, _metadata_http, _agent_http, _service_identity_cache

    async with _http_client_lock:
        clients = [_http, _metadata_http, _agent_http]
        _http = None
        _metadata_http = None
        _agent_http = None
        _service_identity_cache = None

    for client in clients:
        if client is not None:
            await client.aclose()


async def _service_headers(
    *,
    user_sub: str | None = None,
    api_token: str | None = None,
) -> dict[str, str]:
    """Build trusted MCP→Django service-auth headers."""

    headers: dict[str, str] = {
        "X-Validibot-Source": "MCP",
    }
    if settings.mcp_service_key:
        headers["X-MCP-Service-Key"] = settings.mcp_service_key
    else:
        token = await _get_service_identity_token()
        headers["Authorization"] = f"Bearer {token}"
    if user_sub:
        headers["X-Validibot-User-Sub"] = user_sub
    if api_token:
        headers["X-Validibot-Api-Token"] = api_token
    return headers


class APIError(MCPToolError):
    """Raised when the Validibot API returns a non-2xx response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(message=detail, code="API_ERROR")

    def to_error_dict(self) -> dict[str, Any]:
        """Return the standard MCP error payload for an API failure."""

        if self.status_code == 402:
            return {
                "error": {
                    "code": "PAYMENT_REQUIRED",
                    "message": self.detail,
                    "data": {"status_code": self.status_code},
                },
            }
        return {
            "error": {
                "code": self.code,
                "message": self.detail,
                "data": {"status_code": self.status_code},
            },
        }


def _raise_for_status(response: httpx.Response) -> None:
    """Raise APIError with the response body if the status is not 2xx."""
    if response.is_success:
        return
    try:
        detail = response.json()
    except Exception:
        detail = response.text
    raise APIError(response.status_code, str(detail))


async def list_authenticated_workflows(
    *,
    user_sub: str | None = None,
    api_token: str | None = None,
) -> list[dict[str, Any]]:
    """List authenticated MCP workflows without requiring an org slug."""

    if not user_sub and not api_token:
        msg = "user_sub or api_token is required for authenticated workflow discovery."
        raise ValueError(msg)

    http_client = await _get_http_client()
    r = await http_client.get(
        "/api/v1/mcp/workflows/",
        headers=await _service_headers(
            user_sub=user_sub,
            api_token=api_token,
        ),
    )
    _raise_for_status(r)
    return r.json()


async def get_authenticated_workflow_detail(
    workflow_ref: str,
    *,
    user_sub: str | None = None,
    api_token: str | None = None,
) -> dict[str, Any]:
    """Fetch full workflow detail through the authenticated MCP helper API."""

    if not user_sub and not api_token:
        msg = "user_sub or api_token is required for authenticated workflow detail."
        raise ValueError(msg)

    http_client = await _get_http_client()
    r = await http_client.get(
        f"/api/v1/mcp/workflows/{workflow_ref}/",
        headers=await _service_headers(
            user_sub=user_sub,
            api_token=api_token,
        ),
    )
    _raise_for_status(r)
    return r.json()


async def start_authenticated_validation_run(
    workflow_ref: str,
    *,
    file_content_b64: str,
    file_name: str,
    user_sub: str | None = None,
    api_token: str | None = None,
) -> dict[str, Any]:
    """Create a member-access validation run through the MCP helper API."""

    if not user_sub and not api_token:
        msg = "user_sub or api_token is required for authenticated validation runs."
        raise ValueError(msg)

    http_client = await _get_http_client()
    r = await http_client.post(
        f"/api/v1/mcp/workflows/{workflow_ref}/runs/",
        headers=await _service_headers(
            user_sub=user_sub,
            api_token=api_token,
        ),
        json={
            "content": file_content_b64,
            "content_encoding": "base64",
            "filename": file_name,
        },
    )
    _raise_for_status(r)
    return r.json()


async def get_authenticated_run(
    run_ref: str,
    *,
    user_sub: str | None = None,
    api_token: str | None = None,
) -> dict[str, Any]:
    """Fetch member-access run detail through the MCP helper API."""

    if not user_sub and not api_token:
        msg = "user_sub or api_token is required for authenticated run detail."
        raise ValueError(msg)

    http_client = await _get_http_client()
    r = await http_client.get(
        f"/api/v1/mcp/runs/{run_ref}/",
        headers=await _service_headers(
            user_sub=user_sub,
            api_token=api_token,
        ),
    )
    _raise_for_status(r)
    return r.json()


# ── Agent Endpoints (anonymous path) ──────────────────────────────────
#
# These methods call the cloud agent API endpoints. They use service-to-
# service auth (OIDC or shared secret) instead of a user Bearer token.
# The agent's identity is established by x402 payment, not by a user account.


async def build_agent_headers(
    *,
    txhash: str = "",
    wallet: str = "",
    amount: str = "",
    network: str = "",
    asset: str = "",
    pay_to: str = "",
    workflow_slug: str = "",
    org_slug: str = "",
    file_name: str = "",
) -> dict[str, str]:
    """Build HTTP headers for agent API requests with service auth.

    The ``pay_to`` argument carries the receiving wallet that the
    on-chain transfer was sent to.  The Django side compares this
    header against ``settings.X402_PAY_TO_ADDRESS`` and refuses the
    run on mismatch — without the header, the auth layer 401s the
    request as malformed (every x402 header is required).
    """

    h = await _service_headers()

    # x402 payment details.
    if txhash:
        h["X-X402-TxHash"] = txhash
    if wallet:
        h["X-X402-Wallet"] = wallet
    if amount:
        h["X-X402-Amount"] = amount
    if network:
        h["X-X402-Network"] = network
    if asset:
        h["X-X402-Asset"] = asset
    if pay_to:
        h["X-X402-Pay-To"] = pay_to
    if workflow_slug:
        h["X-X402-Workflow-Slug"] = workflow_slug
    if org_slug:
        h["X-X402-Org-Slug"] = org_slug
    if file_name:
        h["X-X402-File-Name"] = file_name

    return h


async def list_agent_workflows() -> list[dict[str, Any]]:
    """List workflows available for anonymous agent access (cross-org).

    Calls GET /api/v1/agent/workflows/ — no user auth needed.
    Returns all workflows where agent_access_enabled=True
    AND agent_billing_mode=agent_pays_x402, across all orgs.
    """
    http_client = await _get_agent_http()
    r = await http_client.get("/api/v1/agent/workflows/")
    _raise_for_status(r)
    return r.json()


async def get_agent_workflow_detail(workflow_ref: str) -> dict[str, Any]:
    """Fetch full detail for a public x402 workflow."""

    http_client = await _get_agent_http()
    r = await http_client.get(f"/api/v1/agent/workflows/{workflow_ref}/")
    _raise_for_status(r)
    return r.json()


async def create_agent_run(
    *,
    txhash: str,
    wallet: str,
    amount: str,
    network: str,
    asset: str,
    pay_to: str,
    workflow_slug: str,
    org_slug: str,
    file_name: str,
    file_content_b64: str,
) -> dict[str, Any]:
    """Create a paid validation run for an anonymous agent.

    Calls POST /api/v1/agent/runs/ with service auth and x402 headers.
    The file content goes in the request body (too large for headers).

    ``pay_to`` is the receiving wallet on the receipt — the Django
    side compares it to its own ``X402_PAY_TO_ADDRESS`` setting and
    refuses runs whose receipts didn't pay this server's wallet.
    Pass ``settings.x402_pay_to_address`` from the caller (the
    validate tool); this client is the trust boundary that asserts
    the value matches the receipt the facilitator confirmed.

    Returns:
        Dict with ``run_id``, ``wallet_address``, ``state``.
    """
    http_client = await _get_agent_http()
    headers = await build_agent_headers(
        txhash=txhash,
        wallet=wallet,
        amount=amount,
        network=network,
        asset=asset,
        pay_to=pay_to,
        workflow_slug=workflow_slug,
        org_slug=org_slug,
        file_name=file_name,
    )
    r = await http_client.post(
        "/api/v1/agent/runs/",
        headers=headers,
        json={"file_content": file_content_b64},
    )
    _raise_for_status(r)
    return r.json()


async def get_agent_run_status(
    run_id: str,
    wallet_address: str,
) -> dict[str, Any]:
    """Get status of an agent-initiated run using dual-key lookup.

    Calls GET /api/v1/agent/runs/{id}/?wallet_address=... — no user
    auth needed. Both the run UUID and wallet address must match.

    Returns:
        Dict with ``run_id``, ``wallet_address``, ``state``, ``result``,
        and optionally ``findings`` (if run is complete).
    """
    http_client = await _get_agent_http()
    r = await http_client.get(
        f"/api/v1/agent/runs/{run_id}/",
        params={"wallet_address": wallet_address},
    )
    _raise_for_status(r)
    return r.json()


async def _get_service_identity_token() -> str:
    """Return a cached Cloud Run identity token for the Django helper API."""

    global _service_identity_cache

    now = time.monotonic()
    cached = _service_identity_cache
    if cached is not None and cached[1] > now:
        return cached[0]

    async with _service_identity_lock:
        cached = _service_identity_cache
        now = time.monotonic()
        if cached is not None and cached[1] > now:
            return cached[0]

        token = await _fetch_service_identity_token(
            settings.effective_mcp_service_audience,
        )
        _service_identity_cache = (
            token,
            now + _token_cache_ttl_seconds(token),
        )
        return token


async def _fetch_service_identity_token(audience: str) -> str:
    """Fetch a Cloud Run identity token from the metadata server."""

    try:
        metadata_http_client = await _get_metadata_http_client()
        response = await metadata_http_client.get(
            _METADATA_IDENTITY_URL,
            params={
                "audience": audience,
                "format": "full",
            },
        )
    except httpx.HTTPError as exc:
        raise APIError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Could not fetch the MCP service identity token from the metadata server.",
        ) from exc

    _raise_for_status(response)
    token = response.text.strip()
    if not token:
        raise APIError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Metadata server returned an empty service identity token.",
        )
    return token


def _token_cache_ttl_seconds(token: str) -> float:
    """Return a conservative cache TTL for a service identity token."""

    try:
        payload_segment = token.split(".")[1]
        padding = "=" * (-len(payload_segment) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(
                f"{payload_segment}{padding}".encode("ascii"),
            ).decode("utf-8"),
        )
        exp = int(payload["exp"])
        ttl_seconds = exp - int(time.time()) - 60
        if ttl_seconds > 0:
            return float(ttl_seconds)
    except (IndexError, KeyError, ValueError) as exc:
        logger.warning(
            "Falling back to the default service identity token TTL because the "
            "metadata-server token could not be parsed: %s",
            exc,
        )
    return float(_SERVICE_TOKEN_CACHE_TTL_SECONDS)
