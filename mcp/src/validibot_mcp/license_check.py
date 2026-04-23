"""
Startup license gate for the Validibot MCP server.

The MCP server is a commercial feature. It only runs when the Validibot
deployment it fronts has the ``mcp_server`` feature enabled — i.e. when
``validibot-pro`` or ``validibot-enterprise`` has been installed into
that deployment's Python environment and registered its license at
import time (see ``validibot.core.license`` for the mechanism).

Rather than import Django just to check the license, the MCP server
asks the community REST API it already talks to. The endpoint is
``GET /api/v1/license/features/``; see
``validibot.core.api.license_views.LicenseFeaturesView`` on the
Django side.

Policy
------

The gate fails closed:

* If the community API is unreachable at boot, the MCP server refuses
  to start. MCP has no useful mode without the REST API, so refusing
  early is more honest than serving traffic that will fail on every
  tool call.
* If the ``mcp_server`` feature is absent from the response, the
  server aborts with a clear message pointing at
  https://validibot.com/pricing.

The check is advisory, not cryptographic — the underlying licensing
model is "installing the commercial package activates the features",
and the API endpoint simply reflects whichever ``License`` object the
community `set_license` call installed. A determined operator could
bypass the gate by editing the code, but doing so forfeits support,
updates, and legal safety under the project's AGPL licence. The gate
exists to communicate the commercial boundary to honest operators,
not to stop adversarial bypass.
"""

from __future__ import annotations

import logging

import httpx

from validibot_mcp.config import get_settings

logger = logging.getLogger(__name__)

# Feature identifier must match ``CommercialFeature.MCP_SERVER.value`` in the
# community codebase (``validibot/core/features.py``). Kept in sync by
# convention; mismatch would surface at first boot with a clear error.
_REQUIRED_FEATURE = "mcp_server"

# Timeout for the single license-check request. 5 seconds is generous for a
# sibling Cloud Run service on a warm connection, and still bounds startup
# time when the community API is misconfigured.
_LICENSE_CHECK_TIMEOUT_SECONDS = 5.0


class LicenseCheckError(RuntimeError):
    """Raised from the ASGI lifespan when the MCP feature is not licensed."""


async def verify_license_or_die() -> None:
    """Verify the deployment licenses the MCP server, or abort startup.

    Called from the server's ASGI lifespan before any tool handlers see
    traffic. A successful call logs the feature set and returns; any
    failure raises :class:`LicenseCheckError` which Starlette surfaces
    as a startup failure (the process exits non-zero so the supervisor
    — Cloud Run, Docker, or systemd — retries with visible logs).
    """

    settings = get_settings()
    url = f"{settings.api_base_url.rstrip('/')}/api/v1/license/features/"

    try:
        async with httpx.AsyncClient(timeout=_LICENSE_CHECK_TIMEOUT_SECONDS) as http:
            response = await http.get(url)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPError as exc:
        raise LicenseCheckError(
            f"MCP license check failed: unable to reach {url} ({exc}). "
            "Is the Validibot API up and VALIDIBOT_API_BASE_URL pointed at it?"
        ) from exc

    features = payload.get("features", [])
    if _REQUIRED_FEATURE not in features:
        edition = payload.get("edition", "community")
        raise LicenseCheckError(
            f"Validibot MCP requires a Pro or Enterprise licence "
            f"(current edition: {edition!r}). Install validibot-pro into "
            f"your Validibot deployment to activate the mcp_server feature. "
            f"See https://validibot.com/pricing for details."
        )

    logger.info(
        "MCP license check passed (edition=%s, features=%s)",
        payload.get("edition", "unknown"),
        sorted(features),
    )
