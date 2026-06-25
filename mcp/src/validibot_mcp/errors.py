"""Structured exception types for MCP tool handlers.

The MCP tool layer should return stable machine-readable error payloads rather
than leaking transport or Python exceptions to the client. Centralizing the
shared error contract here keeps tool modules small and lets new error types
participate in the same formatting path without touching every tool.
"""

from __future__ import annotations

from typing import Any


class MCPToolError(Exception):
    """Base class for exceptions that should become MCP error payloads.

    Subclasses set the machine-readable ``code`` and optionally attach
    additional response ``data``. Tool handlers catch this base class and
    delegate to ``to_error_dict()`` so new error types remain open for
    extension without changing each tool.
    """

    default_code = "INTERNAL_ERROR"
    default_message = "An unexpected error occurred. Please try again."

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.code = code or self.default_code
        self.message = message or self.default_message
        self.data = data or {}
        super().__init__(self.message)

    def to_error_dict(self) -> dict[str, Any]:
        """Return the standard MCP error shape for this exception."""

        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.data:
            error["data"] = self.data
        return {"error": error}
