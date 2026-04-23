"""Tests for MCP dispatch between authenticated helpers and x402 routes.

These tests verify the current contract after the org-first compatibility
paths were removed from the MCP tool surface:

1. ``list_workflows()`` dispatches to the authenticated helper catalog when a
   bearer credential is present, otherwise to the public x402 catalog.
2. ``validate_file(workflow_ref=...)`` dispatches to the authenticated helper
   run launcher for member-access workflows and to the x402 path otherwise.
3. ``get_run_status(run_ref=...)`` dispatches by decoding the opaque run
   reference rather than by requiring org or wallet inputs from the caller.
"""

from __future__ import annotations

import base64

import pytest

from validibot_mcp.refs import build_member_run_ref, build_workflow_ref, build_x402_run_ref
from validibot_mcp.tools.runs import get_run_status
from validibot_mcp.tools.validate import validate_file
from validibot_mcp.tools.workflows import list_workflows

from .conftest import SAMPLE_API_KEY, SAMPLE_WORKFLOW_FULL, SAMPLE_WORKFLOW_SLIM

ORG = "acme-corp"


@pytest.fixture()
def authenticated(monkeypatch):
    """Simulate an authenticated MCP request with a manual bearer token."""

    monkeypatch.setattr("validibot_mcp.auth.get_api_key_or_none", lambda: SAMPLE_API_KEY)
    monkeypatch.setattr(
        "validibot_mcp.auth.get_authenticated_user_sub_or_none",
        lambda: None,
    )


@pytest.fixture()
def anonymous(monkeypatch):
    """Simulate an anonymous MCP request without a bearer credential."""

    monkeypatch.setattr("validibot_mcp.auth.get_api_key_or_none", lambda: None)
    monkeypatch.setattr(
        "validibot_mcp.auth.get_authenticated_user_sub_or_none",
        lambda: None,
    )


class TestListWorkflowsDispatch:
    """Verify workflow discovery uses the correct backing endpoint."""

    async def test_authenticated_hits_mcp_catalog_endpoint(
        self,
        authenticated,
        mock_api,
        monkeypatch,
    ):
        """Authenticated discovery should use the MCP helper catalog."""

        monkeypatch.setattr(
            "validibot_mcp.auth.get_authenticated_user_sub_or_none",
            lambda: "user-sub-123",
        )
        mock_api.get("/api/v1/mcp/workflows/").respond(json=[SAMPLE_WORKFLOW_SLIM])

        result = await list_workflows()

        assert isinstance(result, list)
        assert len(result) == 1

    async def test_anonymous_hits_agent_endpoint(self, anonymous, mock_api):
        """Anonymous discovery should use the public x402 workflow catalog."""

        mock_api.get("/api/v1/agent/workflows/").respond(json=[SAMPLE_WORKFLOW_SLIM])

        result = await list_workflows()

        assert isinstance(result, list)


class TestValidateFileDispatch:
    """Verify validation dispatch uses helper runs or x402 as appropriate."""

    async def test_authenticated_member_access_uses_mcp_helper_run_launcher(
        self,
        authenticated,
        mock_api,
    ):
        """Member-access workflows should launch through the helper endpoint."""

        workflow_ref = build_workflow_ref(org_slug=ORG, workflow_slug="energy-check")
        b64 = base64.b64encode(b"test content").decode()

        mock_api.get(f"/api/v1/mcp/workflows/{workflow_ref}/").respond(
            json={
                **SAMPLE_WORKFLOW_FULL,
                "workflow_ref": workflow_ref,
                "org_slug": ORG,
                "access_modes": ["member_access"],
            },
        )
        mock_api.post(f"/api/v1/mcp/workflows/{workflow_ref}/runs/").respond(
            json={
                "id": "run-123",
                "run_id": "run-123",
                "state": "PENDING",
                "result": "UNKNOWN",
                "run_ref": build_member_run_ref(org_slug=ORG, run_id="run-123"),
            },
        )

        result = await validate_file(
            workflow_ref=workflow_ref,
            file_content=b64,
            file_name="test.json",
        )

        assert result.get("run_ref") == build_member_run_ref(
            org_slug=ORG,
            run_id="run-123",
        )

    async def test_anonymous_no_payment_returns_payment_required(
        self,
        anonymous,
        mock_api,
        monkeypatch,
    ):
        """Public x402 workflows should challenge when no payment is supplied."""

        workflow_ref = build_workflow_ref(org_slug=ORG, workflow_slug="energy-check")
        mock_api.get(f"/api/v1/agent/workflows/{workflow_ref}/").respond(
            json={
                **SAMPLE_WORKFLOW_SLIM,
                "workflow_ref": workflow_ref,
                "org_slug": ORG,
                "agent_price_cents": 10,
                "description": "Test workflow",
                "agent_billing_mode": "AGENT_PAYS_X402",
            },
        )
        monkeypatch.setattr("validibot_mcp.auth.get_payment_signature", lambda: None)

        b64 = base64.b64encode(b"test content").decode()
        result = await validate_file(
            workflow_ref=workflow_ref,
            file_content=b64,
            file_name="test.json",
        )

        assert result["error"]["code"] == "PAYMENT_REQUIRED"
        assert "x402Version" in result["error"]["data"]


class TestGetRunStatusDispatch:
    """Verify run polling dispatches by run_ref kind."""

    async def test_authenticated_member_run_hits_mcp_helper(self, authenticated, mock_api):
        """Member run refs should use the authenticated helper endpoint."""

        run_ref = build_member_run_ref(
            org_slug=ORG,
            run_id="550e8400-e29b-41d4-a716-446655440000",
        )
        mock_api.get(f"/api/v1/mcp/runs/{run_ref}/").respond(
            json={"id": "550e8400-e29b-41d4-a716-446655440000", "state": "PENDING"},
        )

        result = await get_run_status(run_ref=run_ref)

        assert result["state"] == "PENDING"

    async def test_anonymous_x402_run_hits_agent_endpoint(
        self,
        anonymous,
        mock_api,
    ):
        """x402 run refs should continue using the agent polling endpoint."""

        run_id = "550e8400-e29b-41d4-a716-446655440000"
        wallet = "0xMYWALLET"
        mock_api.get(f"/api/v1/agent/runs/{run_id}/").respond(
            json={"run_id": run_id, "wallet_address": wallet, "state": "PENDING"},
        )

        result = await get_run_status(
            run_ref=build_x402_run_ref(run_id=run_id, wallet_address=wallet),
        )

        assert result["state"] == "PENDING"

    async def test_missing_run_ref_returns_error(self, anonymous):
        """Run polling should require the opaque run_ref contract."""

        result = await get_run_status()

        assert result["error"]["code"] == "INVALID_PARAMS"
