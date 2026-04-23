"""
End-to-end tests against the live MCP server.

These tests hit the real deployed MCP server at ``mcp.validibot.com``
with a real API token. They verify the entire production stack: Cloud Run,
FastMCP transport, ``ValidibotTokenVerifier``, and the Validibot REST API.

**These tests are skipped by default.** To run them::

    # Set your real API token
    export VALIDIBOT_E2E_TOKEN="your-api-key"

    # Optionally override the MCP server URL (defaults to production)
    export VALIDIBOT_E2E_URL="https://mcp.validibot.com"

    # Run only E2E tests
    uv run pytest tests/test_e2e.py -xvs

    # Run with a specific org
    export VALIDIBOT_E2E_ORG_SLUG="your-org-slug"
    uv run pytest tests/test_e2e.py -xvs

The tests exercise the full MCP JSON-RPC flow: initialize → tool call →
parse SSE response. They are intentionally read-only (no file submissions)
to avoid creating real validation runs in production.
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

# ── Skip unless opted in ──────────────────────────────────────────────
# These tests require a real Validibot API token and network access to a
# live MCP server (defaults to production at mcp.validibot.com). They
# exercise the full stack — Cloud Run, FastMCP transport, token
# verification, and the Validibot REST API — but are read-only (no file
# submissions) to avoid creating real validation runs.
#
# To run:
#   export VALIDIBOT_E2E_TOKEN="your-validibot-api-token"
#   uv run pytest tests/test_e2e.py -xvs
#
# Optional overrides:
#   VALIDIBOT_E2E_URL      — MCP server URL (default: https://mcp.validibot.com)
#   VALIDIBOT_E2E_ORG_SLUG — org to test against (default: first available)
#
# The token is a standard Validibot REST API Bearer token (the same one
# you'd use with curl). It's stored in .envs/.local/.test because the
# test runner executes locally on a developer machine, even though it
# calls a remote server. The "local" in the path refers to the runner,
# not the target.
#
# This token uses the legacy ValidibotTokenVerifier path (not OIDCProxy)
# for simplicity in E2E testing — we don't want to run a full OAuth
# browser flow in an automated test.

E2E_TOKEN = os.environ.get("VALIDIBOT_E2E_TOKEN")
E2E_URL = os.environ.get("VALIDIBOT_E2E_URL", "https://mcp.validibot.com")
pytestmark = pytest.mark.skipif(
    not E2E_TOKEN,
    reason="VALIDIBOT_E2E_TOKEN not set — skipping E2E tests",
)

# ── Constants ─────────────────────────────────────────────────────────

MCP_ENDPOINT = "/mcp"
CONTENT_TYPE = "application/json"
ACCEPT = "application/json, text/event-stream"


# ── Helpers ───────────────────────────────────────────────────────────


def _jsonrpc(method: str, params: dict | None = None, *, id: int = 1) -> dict:
    """Build a JSON-RPC 2.0 request."""
    msg: dict = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def _auth_headers(*, include_token: bool = True) -> dict:
    """Build standard MCP request headers."""
    headers = {
        "Content-Type": CONTENT_TYPE,
        "Accept": ACCEPT,
    }
    if include_token and E2E_TOKEN:
        headers["Authorization"] = f"Bearer {E2E_TOKEN}"
    return headers


def _parse_sse_result(response: httpx.Response) -> dict:
    """Extract the JSON-RPC result from an SSE response.

    Handles both direct JSON responses and SSE event streams.
    Raises AssertionError with diagnostics on failure.
    """
    assert response.status_code == 200, f"HTTP {response.status_code}: {response.text[:500]}"

    # Try SSE format first (data: {...})
    for line in response.text.strip().splitlines():
        if line.startswith("data: "):
            data = json.loads(line[6:])
            if "result" in data:
                return data["result"]
            if "error" in data:
                pytest.fail(f"JSON-RPC error: {data['error']}")

    # Try direct JSON
    try:
        data = response.json()
        if "result" in data:
            return data["result"]
    except json.JSONDecodeError:
        pass

    pytest.fail(f"Could not parse response: {response.text[:500]}")


def _extract_tool_content(result: dict) -> dict | list:
    """Extract the tool's return value from the MCP result wrapper.

    FastMCP may return structuredContent, text content, or direct content.
    """
    # structuredContent (FastMCP 3.x)
    structured = result.get("structuredContent")
    if structured is not None:
        if "result" in structured and isinstance(structured["result"], (dict, list)):
            return structured["result"]
        return structured

    # Text content
    content = result.get("content", [])
    if content and content[0].get("type") == "text":
        return json.loads(content[0]["text"])

    return result


# ── E2E tests ─────────────────────────────────────────────────────────


class TestE2EHealth:
    """Basic connectivity and health checks against the live server."""

    def test_root_returns_service_info(self):
        """GET / should return service info JSON (no auth required)."""
        response = httpx.get(f"{E2E_URL}/", timeout=10)
        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "validibot-mcp"
        assert data["mcp_endpoint"] == "/mcp"
        assert data["public_mcp_endpoint"] == "/public-mcp"

    def test_unauthenticated_request_returns_401(self):
        """POST /mcp without a Bearer token should return 401."""
        response = httpx.post(
            f"{E2E_URL}{MCP_ENDPOINT}",
            json=_jsonrpc(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "e2e-test", "version": "1.0"},
                },
            ),
            headers=_auth_headers(include_token=False),
            timeout=10,
        )
        assert response.status_code == 401, (
            f"Expected 401 without auth, got {response.status_code}"
        )


class TestE2EInitialize:
    """MCP initialization handshake against the live server."""

    def test_initialize_returns_session_id(self):
        """A valid initialize request should return 200 with a session ID."""
        response = httpx.post(
            f"{E2E_URL}{MCP_ENDPOINT}",
            json=_jsonrpc(
                "initialize",
                {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "e2e-test", "version": "1.0"},
                },
            ),
            headers=_auth_headers(),
            timeout=10,
        )
        assert response.status_code == 200, f"Initialize failed: {response.text[:500]}"
        assert response.headers.get("mcp-session-id"), "No session ID in response"


class TestE2EListWorkflows:
    """list_workflows tool against the live server.

    This is a read-only operation that lists available workflows.
    """

    def _initialize_and_call_tool(
        self,
        tool_name: str,
        arguments: dict,
    ) -> dict | list:
        """Initialize an MCP session and call a tool in one flow."""
        with httpx.Client(base_url=E2E_URL, timeout=30) as client:
            # Step 1: Initialize
            init_response = client.post(
                MCP_ENDPOINT,
                json=_jsonrpc(
                    "initialize",
                    {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "e2e-test", "version": "1.0"},
                    },
                ),
                headers=_auth_headers(),
            )
            assert init_response.status_code == 200, (
                f"Initialize failed: {init_response.text[:500]}"
            )
            session_id = init_response.headers["mcp-session-id"]

            # Step 2: Call tool
            tool_headers = _auth_headers()
            tool_headers["Mcp-Session-Id"] = session_id

            tool_response = client.post(
                MCP_ENDPOINT,
                json=_jsonrpc(
                    "tools/call",
                    {"name": tool_name, "arguments": arguments},
                    id=2,
                ),
                headers=tool_headers,
            )

            result = _parse_sse_result(tool_response)
            return _extract_tool_content(result)

    def test_list_workflows_returns_list(self):
        """list_workflows should return a list (possibly empty) without errors."""
        content = self._initialize_and_call_tool(
            "list_workflows",
            {},
        )

        # Should be a list of workflows (or an empty list)
        if isinstance(content, dict) and "error" in content:
            pytest.fail(f"Tool returned error: {content['error']}")

        # If it's a list, each item should have at least a slug
        if isinstance(content, list) and len(content) > 0:
            assert "slug" in content[0], f"Workflow missing 'slug': {content[0]}"

    def test_list_workflows_with_invalid_token_returns_error(self):
        """list_workflows with a bogus token should fail at the auth layer."""
        with httpx.Client(base_url=E2E_URL, timeout=30) as client:
            # Initialize with the bogus token
            init_headers = {
                "Content-Type": CONTENT_TYPE,
                "Accept": ACCEPT,
                "Authorization": "Bearer obviously-invalid-token-12345",
            }
            init_response = client.post(
                MCP_ENDPOINT,
                json=_jsonrpc(
                    "initialize",
                    {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "e2e-test", "version": "1.0"},
                    },
                ),
                headers=init_headers,
            )

            # Should be rejected — either at transport (401) or tool level (error response)
            if init_response.status_code == 401:
                return  # Good — rejected at transport

            # If initialize succeeded (some auth providers accept any token
            # for initialize), the tool call should fail
            session_id = init_response.headers.get("mcp-session-id", "")
            tool_headers = {
                **init_headers,
                "Mcp-Session-Id": session_id,
            }
            tool_response = client.post(
                MCP_ENDPOINT,
                json=_jsonrpc(
                    "tools/call",
                    {"name": "list_workflows", "arguments": {}},
                    id=2,
                ),
                headers=tool_headers,
            )

            # Should either be HTTP 401 or a tool-level UNAUTHORIZED error
            if tool_response.status_code == 401:
                return

            result = _parse_sse_result(tool_response)
            content = _extract_tool_content(result)
            assert isinstance(content, dict) and content.get("error"), (
                f"Expected error with invalid token, got: {content}"
            )
