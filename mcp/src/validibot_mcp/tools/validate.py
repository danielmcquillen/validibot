"""
File validation tool: validate_file.

This is the primary tool agents use — they submit a file for validation against
one of the caller's workflows and receive a run reference to track the results.

MCP agents always act on behalf of an authenticated user: a Bearer credential
(OAuth 2.1 access token or legacy Validibot API token) is required. The run is
launched through the authenticated MCP helper API and billed to the user's plan
quota — there is no anonymous or payment-backed path on this surface.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from validibot_mcp import auth, client
from validibot_mcp.errors import MCPToolError
from validibot_mcp.gating import check_global_enabled
from validibot_mcp.tools import format_error

if TYPE_CHECKING:
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# 10 MB encoded — matches the existing REST API upload limit.
_MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024


_VALIDATE_TOOL_ANNOTATIONS = {
    "readOnlyHint": False,
    "idempotentHint": False,
    "destructiveHint": False,
    "openWorldHint": True,
}


async def _validate_authenticated_member(
    *,
    api_key: str,
    user_sub: str | None,
    workflow_ref: str,
    file_content: str,
    file_name: str,
) -> dict[str, Any]:
    """Launch a member-access run through the authenticated MCP helper API."""

    return await client.start_authenticated_validation_run(
        workflow_ref,
        file_content_b64=file_content,
        file_name=file_name,
        user_sub=user_sub,
        api_token=None if user_sub else api_key,
    )


async def validate_file(
    file_content: str,
    file_name: str,
    workflow_ref: str = "",
) -> dict[str, Any]:
    """Submit a file for validation against a workflow.

    The file content must be base64-encoded. Most validation files (IDF,
    JSON, XML, YAML) are text-based and reasonably sized.

    This returns immediately with a run reference. Use ``get_run_status`` or
    ``wait_for_run`` to check results — validation may take seconds to
    minutes depending on the workflow.

    A Bearer credential (OAuth access token or legacy API token) is required:
    validation runs are billed to your plan quota.

    Args:
        file_content: Base64-encoded file content (max 10 MB encoded).
        file_name: Original filename including extension (used for file
            type detection, e.g. "model.idf", "data.json").
        workflow_ref: Opaque workflow handle from ``list_workflows``.

    Returns:
        Dictionary including an opaque ``run_ref`` plus the backend run status.
    """
    try:
        check_global_enabled()

        # Size check before anything else.
        if len(file_content) > _MAX_FILE_SIZE_BYTES:
            return {
                "error": {
                    "code": "INVALID_PARAMS",
                    "message": (
                        f"File content exceeds maximum size of "
                        f"{_MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB (encoded)."
                    ),
                },
            }

        if not workflow_ref:
            return {
                "error": {
                    "code": "INVALID_PARAMS",
                    "message": "workflow_ref is required.",
                },
            }

        # A Bearer token is mandatory — raises AuthenticationError when absent.
        api_key = auth.get_api_key()
        user_sub = auth.get_authenticated_user_sub_or_none()

        return await _validate_authenticated_member(
            api_key=api_key,
            user_sub=user_sub,
            workflow_ref=workflow_ref,
            file_content=file_content,
            file_name=file_name,
        )

    except MCPToolError as exc:
        return format_error(exc)


def register_tools(server: FastMCP) -> None:
    """Register the validation tool on a FastMCP server instance."""

    server.tool(validate_file, annotations=_VALIDATE_TOOL_ANNOTATIONS)
