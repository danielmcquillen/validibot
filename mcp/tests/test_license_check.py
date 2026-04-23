"""
Tests for the MCP startup license gate.

``verify_license_or_die()`` is the boot-time check that prevents a
community-only Validibot deployment from running the MCP server. It
calls the Validibot REST API's ``GET /api/v1/license/features/``
endpoint and only returns when the ``mcp_server`` feature is present.

These tests cover the three outcomes that matter operationally:

1. The feature is licensed — boot proceeds silently.
2. The feature is not licensed — boot raises a ``LicenseCheckError``
   pointing the operator at the pricing page.
3. The API cannot be reached — boot raises a ``LicenseCheckError``
   with a diagnostic that names the URL and the underlying httpx error.

The license-check fixture in ``conftest.py`` patches ``verify_license_or_die``
on the server module so these are the only tests in the suite that exercise
the real implementation.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from validibot_mcp.config import get_settings
from validibot_mcp.license_check import LicenseCheckError, verify_license_or_die

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.fixture()
def api_base_url(monkeypatch) -> str:
    """Point settings at a known test API URL and return it.

    A fixed value keeps the respx routes below deterministic regardless of
    whatever VALIDIBOT_API_BASE_URL is set in the developer's shell.
    """

    url = "https://app.test.validibot.example"
    monkeypatch.setenv("VALIDIBOT_API_BASE_URL", url)
    get_settings.cache_clear()
    assert get_settings().api_base_url == url
    return url


async def test_verify_license_passes_when_feature_is_listed(
    api_base_url: str,
    mock_api: respx.Router,
) -> None:
    """Deployments that advertise mcp_server must boot without error.

    This is the happy path — validibot-pro is installed, the community API
    returns the feature list, and the MCP server proceeds to serve traffic.
    """

    mock_api.get(f"{api_base_url}/api/v1/license/features/").mock(
        return_value=httpx.Response(
            200,
            json={
                "edition": "pro",
                "features": ["billing", "mcp_server", "team_management"],
            },
        ),
    )

    # Must not raise.
    await verify_license_or_die()


async def test_verify_license_rejects_community_only_deployments(
    api_base_url: str,
    mock_api: respx.Router,
) -> None:
    """A deployment without mcp_server in its licence must refuse to boot.

    Community-only deployments (no validibot-pro installed) return an empty
    feature list. The gate must raise a LicenseCheckError that mentions the
    edition — the operator should see immediately why the server will not
    start.
    """

    mock_api.get(f"{api_base_url}/api/v1/license/features/").mock(
        return_value=httpx.Response(
            200,
            json={"edition": "community", "features": []},
        ),
    )

    with pytest.raises(LicenseCheckError) as excinfo:
        await verify_license_or_die()

    assert "community" in str(excinfo.value)
    assert "validibot-pro" in str(excinfo.value)


async def test_verify_license_fails_when_api_is_unreachable(
    api_base_url: str,
    mock_api: respx.Router,
) -> None:
    """Transport failures against the Validibot API must also abort startup.

    The MCP server has no useful mode when the Validibot REST API is down —
    every tool call will fail for the same reason. Failing fast at boot is
    more honest than accepting traffic that will 5xx. The error message
    must name the URL so the operator can verify VALIDIBOT_API_BASE_URL.
    """

    mock_api.get(f"{api_base_url}/api/v1/license/features/").mock(
        side_effect=httpx.ConnectError("connection refused"),
    )

    with pytest.raises(LicenseCheckError) as excinfo:
        await verify_license_or_die()

    assert api_base_url in str(excinfo.value)
    assert "/api/v1/license/features/" in str(excinfo.value)
