"""
Tests for agent access gating logic.

These tests verify the defense-in-depth checks that the MCP server performs
before forwarding requests to the Validibot API. The Django API is the
authoritative gate — these checks are UX improvements and optimizations.
"""

from __future__ import annotations

import pytest

from validibot_mcp.gating import GatingError, check_agent_access, check_global_enabled


class TestCheckGlobalEnabled:
    """Verify the global MCP kill switch behavior."""

    def test_enabled_by_default(self):
        """When MCP_ENABLED is not set, the server should be available."""
        # Default settings have mcp_enabled=True — should not raise.
        check_global_enabled()

    def test_disabled_raises_gating_error(self, monkeypatch):
        """When MCP_ENABLED is False, all tool calls should be rejected."""
        monkeypatch.setenv("VALIDIBOT_MCP_ENABLED", "false")
        # Clear the lru_cache so the new env var is picked up
        from validibot_mcp.config import get_settings

        get_settings.cache_clear()
        try:
            with pytest.raises(GatingError, match="temporarily unavailable"):
                check_global_enabled()
        finally:
            # Restore default
            monkeypatch.delenv("VALIDIBOT_MCP_ENABLED", raising=False)
            get_settings.cache_clear()


class TestCheckAgentAccess:
    """Verify per-workflow agent access checks."""

    def test_enabled_workflow_passes(self):
        """Workflows with agent_access_enabled=True should pass."""
        workflow = {
            "slug": "energy-check",
            "agent_access_enabled": True,
            "agent_public_discovery": True,
        }
        check_agent_access(workflow)  # should not raise

    def test_disabled_workflow_raises(self):
        """Workflows with agent_access_enabled=False should be rejected."""
        workflow = {
            "slug": "private-workflow",
            "agent_access_enabled": False,
            "agent_public_discovery": False,
        }
        with pytest.raises(GatingError, match="does not allow agent access"):
            check_agent_access(workflow)

    def test_missing_field_raises(self):
        """Workflows without the field should be treated as disabled."""
        workflow = {"slug": "old-workflow"}
        with pytest.raises(GatingError, match="does not allow agent access"):
            check_agent_access(workflow)
