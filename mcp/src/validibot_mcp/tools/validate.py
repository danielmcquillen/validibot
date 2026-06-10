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

import logging
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

logger = logging.getLogger(__name__)

# 10 MB encoded — matches the existing REST API upload limit.
_MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024


def _confirm_or_reject(
    *,
    confirmed: str | None,
    expected: str,
    field: str,
    case_insensitive: bool = False,
    required: bool = False,
) -> str:
    """Assert a facilitator-confirmed field equals this server's expectation.

    WHAT: Compares a single facilitator-confirmed settlement field
    (``pay_to`` / ``network`` / ``asset`` / ``amount``) against the value this
    MCP server is configured to expect. On a mismatch it raises
    :class:`PaymentInvalidError` (fail closed). When the facilitator omitted
    the field (``confirmed is None``) the behaviour depends on ``required``:
    a settlement-critical field (``required=True``, used for ``pay_to`` and
    ``amount``) is rejected outright — we will not create a paid run whose
    destination wallet or paid amount cannot be independently confirmed — while
    a non-critical field falls back to the expected value with a warning so a
    sparse-but-valid facilitator response is not rejected over, say, a missing
    ``network`` echo.

    WHY: The facilitator is an independent trust boundary. Asserting the
    confirmed value here — before any run is created — closes the gap where a
    misconfigured or compromised facilitator settles a payment to a different
    wallet, chain, or asset, or for a smaller amount, while every downstream
    Django check (which previously received an echo of local config) still
    passes. We return the value that will be forwarded to Django: the
    confirmed value when present, else the expected fallback.

    Args:
        confirmed: The facilitator-confirmed value, or ``None`` if omitted.
        expected: This server's configured expectation for the field.
        field: Human-readable field name, used only in log/error messages.
        case_insensitive: When ``True``, compare case-insensitively. EVM
            addresses (pay-to, asset contract) are written in EIP-55 mixed
            case but the underlying bytes are equivalent.
        required: When ``True``, an omitted (``None``) confirmation is treated
            as a verification failure and rejected (fail closed) rather than
            substituted with ``expected``. Use for settlement-critical fields
            where a missing confirmation must not be papered over (``pay_to``,
            ``amount``).

    Returns:
        The value to forward to Django — ``confirmed`` when present, else
        ``expected`` (only reachable for non-required fields).

    Raises:
        PaymentInvalidError: If ``confirmed`` is present but does not equal
            ``expected``; or if ``confirmed`` is ``None`` and ``required`` is
            ``True`` (both fail closed).
    """
    if confirmed is None:
        if required:
            logger.warning(
                "x402 facilitator omitted confirmed %s; rejecting payment "
                "(fail closed) — a paid run will not be created without an "
                "independently confirmed value for this settlement-critical "
                "field.",
                field,
            )
            raise PaymentInvalidError(
                "x402 payment verification failed: the facilitator did not "
                f"return a confirmed {field}, so settlement to the expected "
                "destination/amount cannot be independently verified.",
            )
        logger.warning(
            "x402 facilitator omitted confirmed %s; falling back to "
            "configured value %r. Downstream verification is weaker for "
            "this run.",
            field,
            expected,
        )
        return expected

    left = confirmed.strip()
    right = expected.strip()
    matches = left.lower() == right.lower() if case_insensitive else left == right
    if not matches:
        logger.warning(
            "x402 confirmed %s mismatch: facilitator confirmed %r but this "
            "server expects %r. Rejecting payment (fail closed).",
            field,
            confirmed,
            expected,
        )
        raise PaymentInvalidError(
            "x402 payment verification failed: the facilitator-confirmed "
            f"{field} does not match this server's configured value.",
        )
    return confirmed


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

    verified = await verify_payment(
        payment_sig,
        requirements,
    )
    if verified is None:
        raise PaymentInvalidError(
            "x402 payment verification failed. The payment signature "
            "is invalid, insufficient, or for the wrong network/asset.",
        )

    # Anchor the downstream comparison to what the facilitator CONFIRMED.
    # ``verify_payment`` returns the facilitator-confirmed settlement
    # dimensions (pay_to / network / asset / amount). We assert each equals
    # this server's configured expectation and fail closed on any mismatch,
    # then forward the CONFIRMED values to Django. Forwarding local config
    # instead would make Django's pay-to check tautological — a facilitator
    # that settled to a different wallet/chain/asset would slip through.
    expected_amount = str(cents_to_usdc_atomic(price_cents))
    confirmed_pay_to = _confirm_or_reject(
        confirmed=verified.pay_to,
        expected=settings.x402_pay_to_address,
        field="pay_to",
        case_insensitive=True,
        # The destination wallet is THE field a compromised/misconfigured
        # facilitator would alter to divert funds — never accept it unconfirmed.
        required=True,
    )
    confirmed_network = _confirm_or_reject(
        confirmed=verified.network,
        expected=settings.x402_network,
        field="network",
    )
    confirmed_asset = _confirm_or_reject(
        confirmed=verified.asset,
        expected=settings.x402_asset,
        field="asset",
        case_insensitive=True,
    )
    confirmed_amount = _confirm_or_reject(
        confirmed=verified.amount,
        expected=expected_amount,
        field="amount",
        # Underpayment must not slip through on a missing amount echo.
        required=True,
    )

    result = await client.create_agent_run(
        txhash=verified.txhash,
        wallet=verified.wallet or "unknown",
        # Forward the facilitator-CONFIRMED settlement dimensions, not local
        # config, so Django compares its own settings against what actually
        # settled on-chain rather than against an echo of this server's config.
        amount=confirmed_amount,
        network=confirmed_network,
        asset=confirmed_asset,
        pay_to=confirmed_pay_to,
        workflow_slug=resolved_workflow_slug,
        org_slug=resolved_org,
        file_name=file_name,
        file_content_b64=file_content,
    )
    run_id = str(result.get("run_id") or result.get("id") or "").strip()
    wallet_address = str(
        result.get("wallet_address") or verified.wallet or "",
    ).strip()
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
