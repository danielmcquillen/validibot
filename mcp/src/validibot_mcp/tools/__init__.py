"""Shared helpers for MCP tool modules.

The actual tool functions are plain callables that can be registered on more
than one FastMCP server surface. This package re-exports the common error
helpers so tool implementations can focus on business logic rather than
repeatedly formatting exceptions.
"""

from __future__ import annotations

from typing import Any

from validibot_mcp.errors import MCPToolError, PaymentInvalidError, PaymentRequiredError


def format_error(exc: Exception) -> dict[str, Any]:
    """Convert a known exception into the MCP tool error payload."""

    if isinstance(exc, MCPToolError):
        return exc.to_error_dict()
    return MCPToolError().to_error_dict()


__all__ = [
    "MCPToolError",
    "PaymentInvalidError",
    "PaymentRequiredError",
    "format_error",
]
