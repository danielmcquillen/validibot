"""Tests for the MCP tool layer.

These tests cover the tool contract the model sees, not just the lower-level
HTTP client helpers. The current contract is:

- discovery returns ``workflow_ref`` plus display metadata like ``org_slug``
- workflow detail, validation, and polling all route through opaque refs
- authenticated member access goes through MCP helper endpoints
- public x402 access keeps using the anonymous payment-backed endpoints
"""

from __future__ import annotations

import base64

from validibot_mcp.refs import build_member_run_ref, build_workflow_ref, build_x402_run_ref
from validibot_mcp.tools.runs import get_run_status, wait_for_run
from validibot_mcp.tools.validate import _MAX_FILE_SIZE_BYTES, validate_file
from validibot_mcp.tools.workflows import get_workflow_details, list_workflows

from .conftest import (
    SAMPLE_RUN_COMPLETED,
    SAMPLE_RUN_PENDING,
    SAMPLE_WORKFLOW_AGENT_PAYS,
    SAMPLE_WORKFLOW_FULL,
    SAMPLE_WORKFLOW_SLIM,
)

ORG = "acme-corp"


class TestListWorkflowsTool:
    """Verify the workflow discovery tool."""

    async def test_returns_authenticated_catalog(self, mock_auth, mock_api, monkeypatch):
        """Authenticated discovery should use the MCP helper catalog."""

        monkeypatch.setattr(
            "validibot_mcp.auth.get_authenticated_user_sub_or_none",
            lambda: "user-sub-123",
        )
        mock_api.get("/api/v1/mcp/workflows/").respond(
            json=[{**SAMPLE_WORKFLOW_SLIM, "workflow_ref": "wf_demo"}],
        )

        result = await list_workflows()

        assert isinstance(result, list)
        assert result[0]["workflow_ref"] == "wf_demo"

    async def test_no_auth_falls_to_anonymous_catalog(self, monkeypatch, mock_api):
        """Without a bearer token the tool should use the public agent catalog."""

        monkeypatch.setattr("validibot_mcp.auth.get_api_key_or_none", lambda: None)
        mock_api.get("/api/v1/agent/workflows/").respond(json=[])

        result = await list_workflows()

        assert isinstance(result, list)

    async def test_gating_error_returns_structured_dict(self, mock_auth, monkeypatch):
        """The global kill switch should produce a structured error response."""

        monkeypatch.setenv("VALIDIBOT_MCP_ENABLED", "false")

        result = await list_workflows()

        assert result["error"]["code"] == "SERVICE_UNAVAILABLE"


class TestGetWorkflowDetailsTool:
    """Verify the workflow detail tool."""

    async def test_returns_enriched_member_workflow_details(
        self,
        mock_auth,
        mock_api,
        monkeypatch,
    ):
        """Authenticated detail should come from the MCP helper endpoint."""

        workflow_ref = build_workflow_ref(org_slug=ORG, workflow_slug="energy-check")
        monkeypatch.setattr(
            "validibot_mcp.auth.get_authenticated_user_sub_or_none",
            lambda: "user-sub-123",
        )
        mock_api.get(f"/api/v1/mcp/workflows/{workflow_ref}/").respond(
            json={**SAMPLE_WORKFLOW_FULL, "workflow_ref": workflow_ref, "org_slug": ORG},
        )

        result = await get_workflow_details(workflow_ref=workflow_ref)

        assert result["workflow_ref"] == workflow_ref
        assert "accepted_extensions" in result
        assert "pricing" in result
        assert "validation_summary" in result

    async def test_member_workflow_detail_does_not_require_agent_publication(
        self,
        mock_auth,
        mock_api,
    ):
        """Authenticated member workflows should stay visible when not public."""

        workflow_ref = build_workflow_ref(org_slug=ORG, workflow_slug="private-member")
        mock_api.get(f"/api/v1/mcp/workflows/{workflow_ref}/").respond(
            json={
                **SAMPLE_WORKFLOW_FULL,
                "workflow_ref": workflow_ref,
                "org_slug": ORG,
                "agent_access_enabled": False,
                "access_modes": ["member_access"],
            },
        )

        result = await get_workflow_details(workflow_ref=workflow_ref)

        assert result["workflow_ref"] == workflow_ref
        assert result["pricing"]["payment_required"] is False

    async def test_returns_full_anonymous_workflow_detail(self, monkeypatch, mock_api):
        """Anonymous detail should use the public detail endpoint, not the slim list."""

        monkeypatch.setattr("validibot_mcp.auth.get_api_key_or_none", lambda: None)
        workflow_ref = build_workflow_ref(org_slug=ORG, workflow_slug="premium-check")
        mock_api.get(f"/api/v1/agent/workflows/{workflow_ref}/").respond(
            json={**SAMPLE_WORKFLOW_AGENT_PAYS, "workflow_ref": workflow_ref, "org_slug": ORG},
        )

        result = await get_workflow_details(workflow_ref=workflow_ref)

        assert "2 validation steps" in result["validation_summary"]
        assert result["pricing"]["payment_required"] is True

    async def test_public_x402_detail_requires_agent_publication(self, mock_auth, mock_api):
        """Public-only workflows should still require agent publication."""

        workflow_ref = build_workflow_ref(org_slug=ORG, workflow_slug="private-workflow")
        mock_api.get(f"/api/v1/mcp/workflows/{workflow_ref}/").respond(
            json={
                **SAMPLE_WORKFLOW_FULL,
                "workflow_ref": workflow_ref,
                "org_slug": ORG,
                "agent_access_enabled": False,
                "access_modes": ["public_x402"],
            },
        )

        result = await get_workflow_details(workflow_ref=workflow_ref)

        assert result["error"]["code"] == "FORBIDDEN"


class TestValidateFileTool:
    """Verify the validation-launch tool."""

    async def test_submits_member_access_file_successfully(self, mock_auth, mock_api):
        """Member-access launches should go through the helper run endpoint."""

        slug = "energy-check"
        workflow_ref = build_workflow_ref(org_slug=ORG, workflow_slug=slug)
        run_ref = build_member_run_ref(org_slug=ORG, run_id=SAMPLE_RUN_PENDING["id"])
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
            json={**SAMPLE_RUN_PENDING, "run_ref": run_ref},
        )

        result = await validate_file(
            file_content=b64,
            file_name="test.json",
            workflow_ref=workflow_ref,
        )

        assert result["id"] == SAMPLE_RUN_PENDING["id"]
        assert result["run_ref"] == run_ref

    async def test_member_access_launch_ignores_agent_publication_flag(
        self,
        mock_auth,
        mock_api,
    ):
        """Authenticated member launches should work for non-public workflows."""

        workflow_ref = build_workflow_ref(org_slug=ORG, workflow_slug="private-member")
        run_ref = build_member_run_ref(org_slug=ORG, run_id=SAMPLE_RUN_PENDING["id"])
        b64 = base64.b64encode(b"test content").decode()

        mock_api.get(f"/api/v1/mcp/workflows/{workflow_ref}/").respond(
            json={
                **SAMPLE_WORKFLOW_FULL,
                "workflow_ref": workflow_ref,
                "org_slug": ORG,
                "agent_access_enabled": False,
                "access_modes": ["member_access"],
            },
        )
        mock_api.post(f"/api/v1/mcp/workflows/{workflow_ref}/runs/").respond(
            json={**SAMPLE_RUN_PENDING, "run_ref": run_ref},
        )

        result = await validate_file(
            file_content=b64,
            file_name="test.json",
            workflow_ref=workflow_ref,
        )

        assert result["run_ref"] == run_ref

    async def test_oversized_file_returns_error(self, mock_auth):
        """Files over the encoded size limit should fail fast."""

        huge_content = "A" * (_MAX_FILE_SIZE_BYTES + 1)

        result = await validate_file(
            file_content=huge_content,
            file_name="huge.json",
            workflow_ref=build_workflow_ref(org_slug=ORG, workflow_slug="energy-check"),
        )

        assert result["error"]["code"] == "INVALID_PARAMS"

    async def test_member_helper_api_error_returns_structured_error(self, mock_auth, mock_api):
        """Helper endpoint errors should be surfaced in the standard MCP format."""

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
            status_code=403,
            json={"detail": "Forbidden"},
        )

        result = await validate_file(
            file_content=b64,
            file_name="test.json",
            workflow_ref=workflow_ref,
        )

        assert result["error"]["code"] == "API_ERROR"

    async def test_authenticated_public_x402_workflow_uses_payment_path(
        self,
        mock_auth,
        mock_api,
        monkeypatch,
    ):
        """Authenticated callers should still pay for public-only x402 workflows."""

        workflow_ref = build_workflow_ref(org_slug=ORG, workflow_slug="premium-check")
        monkeypatch.setattr(
            "validibot_mcp.auth.get_authenticated_user_sub_or_none",
            lambda: "user-sub-123",
        )
        monkeypatch.setattr("validibot_mcp.auth.get_payment_signature", lambda: None)
        mock_api.get(f"/api/v1/mcp/workflows/{workflow_ref}/").respond(
            json={
                **SAMPLE_WORKFLOW_AGENT_PAYS,
                "workflow_ref": workflow_ref,
                "org_slug": ORG,
                "access_modes": ["public_x402"],
            },
        )

        b64 = base64.b64encode(b"test content").decode()
        result = await validate_file(
            file_content=b64,
            file_name="test.json",
            workflow_ref=workflow_ref,
        )

        assert result["error"]["code"] == "PAYMENT_REQUIRED"


class TestRunTools:
    """Verify status and wait tools."""

    async def test_get_run_status_returns_member_run(self, mock_auth, mock_api):
        """Member run refs should resolve through the authenticated helper route."""

        run_id = SAMPLE_RUN_PENDING["id"]
        run_ref = build_member_run_ref(org_slug=ORG, run_id=run_id)
        mock_api.get(f"/api/v1/mcp/runs/{run_ref}/").respond(
            json={**SAMPLE_RUN_PENDING, "run_ref": run_ref},
        )

        result = await get_run_status(run_ref=run_ref)

        assert result["state"] == "PENDING"
        assert result["run_ref"] == run_ref

    async def test_get_run_status_api_error_returns_structured_dict(self, mock_auth, mock_api):
        """Authenticated helper errors should produce MCP API_ERROR payloads."""

        run_ref = build_member_run_ref(org_slug=ORG, run_id="nonexistent")
        mock_api.get(f"/api/v1/mcp/runs/{run_ref}/").respond(
            status_code=404,
            json={"detail": "Not found"},
        )

        result = await get_run_status(run_ref=run_ref)

        assert result["error"]["code"] == "API_ERROR"

    async def test_wait_for_run_returns_completed_run(self, mock_auth, mock_api):
        """Completed runs should return immediately without polling further."""

        run_id = SAMPLE_RUN_COMPLETED["id"]
        run_ref = build_member_run_ref(org_slug=ORG, run_id=run_id)
        mock_api.get(f"/api/v1/mcp/runs/{run_ref}/").respond(
            json={**SAMPLE_RUN_COMPLETED, "run_ref": run_ref},
        )

        result = await wait_for_run(run_ref=run_ref, timeout_seconds=10)

        assert result["state"] == "COMPLETED"
        assert result["result"] == "PASS"

    async def test_wait_for_run_timeout_returns_timed_out(self, mock_auth, mock_api):
        """Timeout should return the last known run state plus TIMED_OUT."""

        run_id = SAMPLE_RUN_PENDING["id"]
        run_ref = build_member_run_ref(org_slug=ORG, run_id=run_id)
        mock_api.get(f"/api/v1/mcp/runs/{run_ref}/").respond(
            json={**SAMPLE_RUN_PENDING, "run_ref": run_ref},
        )

        result = await wait_for_run(run_ref=run_ref, timeout_seconds=-1)

        assert result["result"] == "TIMED_OUT"
        assert result["is_complete"] is False

    async def test_wait_for_run_returns_when_server_reports_timed_out(self, monkeypatch, mock_api):
        """
        When the server reports a terminal run whose result is
        ``TIMED_OUT``, ``wait_for_run`` must return the snapshot
        immediately rather than polling until the client-side budget
        expires.

        After the wire-format unification, both the authenticated MCP
        path and the anonymous x402 path emit ``state="COMPLETED"``
        for any terminal status, with the granular outcome in
        ``result``. This test exercises that contract on the x402
        path: the server says the run is done
        (``state="COMPLETED", result="TIMED_OUT"``) and the tool must
        surface that snapshot without idling for the client-side
        ``timeout_seconds`` budget.

        Previously the x402 endpoint emitted ``state="TIMED_OUT"`` and
        the MCP terminal-state set didn't include it, so an agent
        calling ``wait_for_run(timeout_seconds=300)`` after a server
        timeout would idle for five minutes and then fabricate its
        own client-side timeout envelope, hiding the real verdict.
        """

        monkeypatch.setattr("validibot_mcp.auth.get_api_key_or_none", lambda: None)
        run_id = SAMPLE_RUN_PENDING["id"]
        wallet_address = "0xMYWALLET"
        run_ref = build_x402_run_ref(
            run_id=run_id,
            wallet_address=wallet_address,
        )
        # Production shape after the fix: ``state`` is the projected
        # lifecycle value; ``result`` carries the granular outcome.
        mock_api.get(f"/api/v1/agent/runs/{run_id}/").respond(
            json={
                "run_id": run_id,
                "wallet_address": wallet_address,
                "state": "COMPLETED",
                "result": "TIMED_OUT",
            },
        )

        # A generous client-side timeout proves the function returns based on
        # the server's terminal state, not because the budget expired.
        result = await wait_for_run(run_ref=run_ref, timeout_seconds=60)

        assert result["state"] == "COMPLETED"
        assert result["result"] == "TIMED_OUT"
        # The client-side timeout helper would have stamped ``is_complete``
        # to ``False``; an early-exit must not.
        assert "is_complete" not in result or result.get("is_complete") is not False

    async def test_x402_run_ref_uses_agent_polling(self, monkeypatch, mock_api):
        """x402 run refs should continue to use the anonymous agent endpoint."""

        monkeypatch.setattr("validibot_mcp.auth.get_api_key_or_none", lambda: None)
        run_id = SAMPLE_RUN_PENDING["id"]
        wallet_address = "0xMYWALLET"
        run_ref = build_x402_run_ref(
            run_id=run_id,
            wallet_address=wallet_address,
        )
        mock_api.get(f"/api/v1/agent/runs/{run_id}/").respond(
            json={
                "run_id": run_id,
                "wallet_address": wallet_address,
                "state": "PENDING",
            },
        )

        result = await get_run_status(run_ref=run_ref)

        assert result["state"] == "PENDING"
        assert result["run_ref"] == run_ref
