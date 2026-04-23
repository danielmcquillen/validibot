"""
Agent access gating — defense-in-depth checks before forwarding to the API.

The authoritative enforcement of ``agent_access_enabled`` happens in the
Django REST API. This module provides a *defense-in-depth* layer that:

1. Checks ``agent_access_enabled`` on the workflow detail response and returns
   a structured FORBIDDEN error immediately — avoiding a wasted round-trip to
   the ``/runs/`` endpoint and producing a better error message for agents.

2. Checks the global kill switch (``MCP_ENABLED`` env var) so the operator
   can shut down all MCP traffic without redeploying.

These checks are optimizations and UX improvements, NOT security boundaries.
The Django API is the authoritative gate.
"""

from __future__ import annotations

from typing import Any

from validibot_mcp.config import get_settings
from validibot_mcp.errors import MCPToolError


class GatingError(MCPToolError):
    """Raised when an agent access check fails."""

    def __init__(self, code: str, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message=message, code=code, data=data)


def check_global_enabled() -> None:
    """Raise GatingError if the global MCP kill switch is off."""
    settings = get_settings()
    if not settings.mcp_enabled:
        raise GatingError(
            code="SERVICE_UNAVAILABLE",
            message=("Validibot MCP service is temporarily unavailable. Please try again later."),
            data={"retry_after_seconds": 3600},
        )


def check_agent_access(workflow: dict[str, Any]) -> None:
    """Raise GatingError if the workflow doesn't allow agent access.

    Args:
        workflow: The workflow dict from the API (slim or full serializer).
    """
    if not workflow.get("agent_access_enabled"):
        raise GatingError(
            code="FORBIDDEN",
            message=(
                "This workflow does not allow agent access. "
                "The workflow author can enable it in workflow settings."
            ),
            data={"workflow_slug": workflow.get("slug")},
        )
