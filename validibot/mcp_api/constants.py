"""Error-code constants for the community MCP helper API.

Matches the machine-readable codes the FastMCP layer expects in the
``code`` field of DRF error payloads. Cloud's agent endpoints use a
parallel ``AgentRunErrorCode`` enum in ``validibot_cloud.agents.constants``
for x402-specific failures.
"""

from __future__ import annotations

from enum import StrEnum


class MCPHelperErrorCode(StrEnum):
    """Machine-readable error codes for the MCP helper API."""

    INVALID_PARAMS = "INVALID_PARAMS"
    NOT_FOUND = "NOT_FOUND"
