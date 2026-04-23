"""
Tests for the shared error formatting helper.

All MCP tools convert exceptions to structured error dicts via
``format_error()``. These tests verify the output shape matches the
ADR's error response specification for each exception type.
"""

from __future__ import annotations

from validibot_mcp.auth import AuthenticationError
from validibot_mcp.client import APIError
from validibot_mcp.gating import GatingError
from validibot_mcp.tools import format_error


class TestFormatGatingError:
    """GatingError should produce the error code, message, and data from the exception."""

    def test_service_unavailable(self):
        """Global kill switch errors include a retry hint."""
        exc = GatingError(
            code="SERVICE_UNAVAILABLE",
            message="Temporarily unavailable.",
            data={"retry_after_seconds": 3600},
        )
        result = format_error(exc)
        assert result["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert result["error"]["data"]["retry_after_seconds"] == 3600

    def test_forbidden(self):
        """Agent access disabled errors include the workflow slug."""
        exc = GatingError(
            code="FORBIDDEN",
            message="Not allowed.",
            data={"workflow_slug": "private"},
        )
        result = format_error(exc)
        assert result["error"]["code"] == "FORBIDDEN"
        assert result["error"]["data"]["workflow_slug"] == "private"


class TestFormatAuthenticationError:
    """AuthenticationError should produce UNAUTHORIZED with the exception message."""

    def test_unauthorized(self):
        """Missing auth should produce a clear UNAUTHORIZED code."""
        exc = AuthenticationError("Missing or invalid Authorization header.")
        result = format_error(exc)
        assert result["error"]["code"] == "UNAUTHORIZED"
        assert "Missing" in result["error"]["message"]


class TestFormatAPIError:
    """APIError should produce API_ERROR with the status code and detail."""

    def test_api_error_with_status(self):
        """API errors should include the HTTP status code in data."""
        exc = APIError(status_code=404, detail="Not found")
        result = format_error(exc)
        assert result["error"]["code"] == "API_ERROR"
        assert result["error"]["data"]["status_code"] == 404
        assert "Not found" in result["error"]["message"]


class TestFormatUnexpectedError:
    """Unexpected exceptions should produce a safe INTERNAL_ERROR."""

    def test_generic_exception(self):
        """Unknown exceptions should not leak internal details."""
        exc = RuntimeError("database connection pool exhausted")
        result = format_error(exc)
        assert result["error"]["code"] == "INTERNAL_ERROR"
        # The internal error message should NOT appear in the response
        assert "database" not in result["error"]["message"]
