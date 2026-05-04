"""
File validation tool: validate_file.

This is the primary revenue-generating tool — agents submit files for
validation against a workflow and receive a run ID to track the results.

Two-path dispatch:
    - **Authenticated member access**: when the selected workflow is available
      through the caller's org memberships, the MCP server forwards to the
      cloud helper endpoint and the run is billed to the user's plan quota.
    - **Authenticated public x402 access**: when the selected workflow is only
      public x402, the MCP server keeps using the payment-backed path even if a
      bearer token is also present.
    - **Anonymous** (no Bearer, PAYMENT-SIGNATURE present): verifies
      the x402 payment via the Coinbase facilitator, then creates the
      run via the agent endpoint. Returns run_id + wallet_address.
    - **Neither**: returns PAYMENT_REQUIRED with x402 requirements so
      the agent knows how to pay.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from validibot_mcp import auth, client
from validibot_mcp.config import get_settings
from validibot_mcp.errors import MCPToolError
from validibot_mcp.gating import check_agent_access, check_global_enabled
from validibot_mcp.refs import build_x402_run_ref
from validibot_mcp.tools import (
    PaymentInvalidError,
    PaymentRequiredError,
    format_error,
)
from validibot_mcp.x402 import (
    build_payment_requirements,
    cents_to_usdc_atomic,
    verify_payment,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

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


async def _validate_x402(
    *,
    workflow: dict[str, Any],
    file_content: str,
    file_name: str,
) -> dict[str, Any]:
    """Launch or challenge the x402-backed validation path."""

    resolved_org = str(workflow.get("org_slug") or workflow.get("org") or "").strip()
    resolved_workflow_slug = str(workflow.get("slug") or "").strip()
    if not resolved_org or not resolved_workflow_slug:
        return {
            "error": {
                "code": "NOT_FOUND",
                "message": "Workflow routing data is missing from the MCP catalog entry.",
            },
        }

    price_cents = workflow.get("agent_price_cents") or 0
    requirements = build_payment_requirements(
        price_cents=price_cents,
        workflow_slug=resolved_workflow_slug,
        workflow_description=workflow.get("description", ""),
    )

    payment_sig = auth.get_payment_signature()
    if not payment_sig:
        raise PaymentRequiredError(
            requirements=requirements,
            message=(
                f"This workflow requires an x402 payment of "
                f"${price_cents / 100:.2f} USDC. Pay and retry with "
                f"a PAYMENT-SIGNATURE header."
            ),
        )

    settings = get_settings()
    if not settings.x402_enabled:
        return {
            "error": {
                "code": "SERVICE_UNAVAILABLE",
                "message": "x402 payments are not enabled on this server.",
            },
        }

    is_valid, txhash, wallet = await verify_payment(
        payment_sig,
        requirements,
    )
    if not is_valid:
        raise PaymentInvalidError(
            "x402 payment verification failed. The payment signature "
            "is invalid, insufficient, or for the wrong network/asset.",
        )

    result = await client.create_agent_run(
        txhash=txhash,
        wallet=wallet or "unknown",
        amount=str(cents_to_usdc_atomic(price_cents)),
        network=settings.x402_network,
        asset=settings.x402_asset,
        # Forward the receiving wallet so Django can compare it to
        # its own ``X402_PAY_TO_ADDRESS`` and refuse runs whose
        # receipts paid a different address — closes the
        # config-drift gap between MCP / facilitator / Django.
        pay_to=settings.x402_pay_to_address,
        workflow_slug=resolved_workflow_slug,
        org_slug=resolved_org,
        file_name=file_name,
        file_content_b64=file_content,
    )
    run_id = str(result.get("run_id") or result.get("id") or "").strip()
    wallet_address = str(result.get("wallet_address") or wallet or "").strip()
    if run_id and wallet_address:
        result = {
            **result,
            "run_ref": build_x402_run_ref(
                run_id=run_id,
                wallet_address=wallet_address,
            ),
        }
    return result


async def validate_file(
    file_content: str,
    file_name: str,
    workflow_ref: str = "",
) -> dict[str, Any]:
    """Submit a file for validation against a workflow.

    The file content must be base64-encoded. Most validation files (IDF,
    JSON, XML, YAML) are text-based and reasonably sized.

    This returns immediately with a run ID. Use ``get_run_status`` or
    ``wait_for_run`` to check results — validation may take seconds to
    minutes depending on the workflow.

    **Authenticated agents** (with API key): validation runs are billed to
    your plan quota. No payment required.

    **Anonymous agents** (no API key): you must include a ``PAYMENT-SIGNATURE``
    header with a valid x402 payment. If missing, you'll receive a
    ``PAYMENT_REQUIRED`` error with the payment requirements.

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

        api_key = auth.get_api_key_or_none()
        user_sub = auth.get_authenticated_user_sub_or_none()

        if api_key is not None:
            workflow = await client.get_authenticated_workflow_detail(
                workflow_ref,
                user_sub=user_sub,
                api_token=None if user_sub else api_key,
            )
        else:
            workflow = await client.get_agent_workflow_detail(workflow_ref)

        access_modes = workflow.get(
            "access_modes",
            ["member_access"] if api_key is not None else ["public_x402"],
        )
        if api_key is None or "member_access" not in access_modes:
            check_agent_access(workflow)

        if api_key is not None and "member_access" in access_modes:
            return await _validate_authenticated_member(
                api_key=api_key,
                user_sub=user_sub,
                workflow_ref=workflow_ref,
                file_content=file_content,
                file_name=file_name,
            )
        return await _validate_x402(
            workflow=workflow,
            file_content=file_content,
            file_name=file_name,
        )

    except MCPToolError as exc:
        return format_error(exc)


def register_tools(server: FastMCP) -> None:
    """Register the validation tool on a FastMCP server instance."""

    server.tool(validate_file, annotations=_VALIDATE_TOOL_ANNOTATIONS)
