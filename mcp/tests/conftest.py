"""
Shared test fixtures for the Validibot MCP server test suite.

Provides mock API responses, authentication helpers, and httpx mocking
via ``respx``. The ``mock_api`` fixture is auto-used to prevent any
real HTTP calls from leaking during tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import respx

from validibot_mcp.client import aclose_http_clients
from validibot_mcp.x402 import aclose_x402_http_client

# ── Sample API response payloads ───────────────────────────────────────
# These mirror the shapes returned by the Validibot REST API serializers.


SAMPLE_WORKFLOW_SLIM = {
    "id": 1,
    "uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "slug": "energy-check",
    "name": "Energy Model Check",
    "version": 1,
    "org": "acme-corp",
    "is_active": True,
    "allowed_file_types": ["json", "text"],
    "agent_access_enabled": True,
    "agent_public_discovery": True,
    "agent_price_cents": 50,
    "url": "https://api.validibot.com/api/v1/orgs/acme-corp/workflows/energy-check/",
}

SAMPLE_WORKFLOW_NO_AGENT = {
    **SAMPLE_WORKFLOW_SLIM,
    "slug": "private-workflow",
    "name": "Private Workflow",
    "agent_access_enabled": False,
    "agent_public_discovery": False,
}

SAMPLE_WORKFLOW_FULL = {
    **SAMPLE_WORKFLOW_SLIM,
    "is_public": False,
    "allow_submission_name": True,
    "allow_submission_meta_data": False,
    "allow_submission_short_description": False,
    "data_retention": "90 days",
    "output_retention": "90 days",
    "success_message": "",
    "description": "Validates EnergyPlus models against best practices.",
    "agent_billing_mode": "AUTHOR_PAYS",
    "agent_max_launches_per_hour": 50,
    "steps": [],
}

# Workflow configured for agent-pays-ACP billing. Agents must provide a
# Stripe Shared Payment Token (SPT) to launch runs on this workflow.
SAMPLE_WORKFLOW_AGENT_PAYS = {
    **SAMPLE_WORKFLOW_FULL,
    "slug": "premium-check",
    "name": "Premium Energy Check",
    "agent_billing_mode": "AGENT_PAYS_X402",
    "agent_price_cents": 100,
    "steps": [
        {
            "id": 1,
            "order": 1,
            "step_number": 1,
            "name": "JSON Schema Validation",
            "description": "Validates against the EnergyPlus schema.",
            "validator": {
                "slug": "json-schema",
                "name": "JSON Schema Validator",
                "validation_type": "JSON_SCHEMA",
                "short_description": "Validates JSON against a schema.",
                "default_ruleset": None,
            },
            "action_type": None,
            "config": {},
            "ruleset": None,
        },
        {
            "id": 2,
            "order": 2,
            "step_number": 2,
            "name": "EnergyPlus Simulation",
            "description": "Runs a full EnergyPlus simulation.",
            "validator": {
                "slug": "energyplus",
                "name": "EnergyPlus",
                "validation_type": "ENERGYPLUS",
                "short_description": "Runs EnergyPlus simulation.",
                "default_ruleset": None,
            },
            "action_type": None,
            "config": {},
            "ruleset": None,
        },
    ],
}

SAMPLE_RUN_PENDING = {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "status": "PENDING",
    "state": "PENDING",
    "result": "UNKNOWN",
    "source": "MCP",
    "org": "acme-corp",
    "workflow_slug": "energy-check",
    "steps": [],
    "error": None,
}

SAMPLE_RUN_COMPLETED = {
    **SAMPLE_RUN_PENDING,
    "status": "SUCCEEDED",
    "state": "COMPLETED",
    "result": "PASS",
    "steps": [
        {
            "step_id": 1,
            "name": "Validate JSON Schema",
            "status": "PASSED",
            "issues": [],
        },
    ],
}

SAMPLE_API_KEY = "test-api-key-abc123"


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
def mock_api():
    """Activate respx to intercept all httpx requests.

    Tests that need specific API responses should add routes to the
    ``respx`` mock within their test body. Any unmocked requests will
    raise an error, preventing accidental real HTTP calls.
    """
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture()
def mock_auth(monkeypatch):
    """Patch ``get_api_key()`` and ``get_api_key_or_none()`` to return a
    test token without needing a real MCP HTTP request context.

    The ``get_api_key_or_none`` mock returns the test API key (simulating
    the authenticated path). Tests that want the anonymous path should
    use a separate fixture or patch ``get_api_key_or_none`` to return None.
    """
    mock_fn = lambda: SAMPLE_API_KEY  # noqa: E731
    monkeypatch.setattr("validibot_mcp.auth.get_api_key", mock_fn)
    monkeypatch.setattr("validibot_mcp.auth.get_api_key_or_none", mock_fn)
    monkeypatch.setattr("validibot_mcp.auth.get_authenticated_user_sub_or_none", lambda: None)


@pytest.fixture()
def mock_access_token(monkeypatch):
    """Mock ``get_access_token()`` to return a fake AccessToken with
    configurable token value.

    ``get_api_key()`` reads the Bearer token from FastMCP's
    ``get_access_token()`` context. This fixture sets up that context
    so tests don't need a real MCP transport or auth provider.

    Returns a factory function that accepts a token string. Call it
    in your test to set up the mock before calling ``get_api_key()``.
    """

    def _set_token(token: str | None):
        if token is None:
            monkeypatch.setattr(
                "validibot_mcp.auth.get_access_token",
                lambda: None,
            )
        else:
            mock_token = MagicMock()
            mock_token.token = token
            monkeypatch.setattr(
                "validibot_mcp.auth.get_access_token",
                lambda: mock_token,
            )

    return _set_token


@pytest.fixture()
def mock_spt(monkeypatch):
    """Patch ``get_stripe_spt()`` to return a test SPT string.

    Returns a factory function. Call it with a token string to set the
    mock, or with None to simulate no SPT header.
    """

    def _set_spt(spt: str | None):
        mock_fn = lambda: spt  # noqa: E731
        monkeypatch.setattr("validibot_mcp.auth.get_stripe_spt", mock_fn)

    return _set_spt


@pytest.fixture(autouse=True)
def _clear_settings_cache(monkeypatch):
    """Clear the pydantic-settings LRU cache between tests.

    Without this, environment variable changes in one test would leak
    into subsequent tests via the cached ``Settings`` singleton.
    """
    import validibot_mcp.client as client_module
    from validibot_mcp.config import get_settings

    get_settings.cache_clear()
    client_module._service_identity_cache = None
    # Most tests exercise the local-dev/shared-secret service-auth path.
    # Production Cloud Run identity tokens are covered by explicit tests that
    # override this default.
    monkeypatch.setattr(client_module.settings, "mcp_service_key", "service-secret")
    monkeypatch.setattr(client_module.settings, "mcp_service_audience", "")
    yield
    client_module._service_identity_cache = None
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
async def _close_mcp_http_clients():
    """Close shared async clients after each test for clean isolation."""

    yield
    await aclose_http_clients()
    await aclose_x402_http_client()


@pytest.fixture(autouse=True, scope="session")
def _skip_license_check():
    """Neutralise the startup license gate for the whole test suite.

    ``verify_license_or_die()`` runs inside the Starlette lifespan whenever
    a test (typically the transport tests) boots the real ASGI app. In
    production it calls ``GET /api/v1/license/features/`` against the
    Validibot API; in tests we would otherwise need either a respx mock
    on every test that spins up the app, or a live Validibot deployment.
    Short-circuiting to ``None`` is simpler and preserves the licensing
    semantics — the gate itself is exercised directly in
    ``test_license_check.py`` where the real implementation is imported
    from ``validibot_mcp.license_check`` rather than via the server module.

    The fixture is session-scoped because the transport tests spin up the
    ASGI lifespan with a module-scoped fixture of their own; a
    function-scoped override wouldn't be active yet when that runs.
    """

    async def _noop() -> None:
        return None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("validibot_mcp.server.verify_license_or_die", _noop)
        yield
