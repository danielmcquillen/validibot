"""
x402 payment protocol integration (v2).

This module handles the x402 payment lifecycle on the MCP server side:

1. **Build requirements** — construct the payment requirements object that
   tells an agent how much to pay, to which wallet, on which network.
2. **Verify payment** — call the Coinbase CDP facilitator to verify that
   a ``PAYMENT-SIGNATURE`` header contains a valid, sufficient payment.
3. **Price conversion** — convert USD cents to USDC atomic units.

The MCP server verifies payments BEFORE calling Django. If verification
fails, the agent gets ``PAYMENT_INVALID`` and no run is created. If
verification succeeds, the txhash and wallet address are forwarded to
Django's agent endpoint for replay-protected run creation.

Protocol version: x402 v2 (Linux Foundation / x402 Foundation, April 2026).
Headers: ``PAYMENT-REQUIRED``, ``PAYMENT-SIGNATURE``, ``PAYMENT-RESPONSE``.
Network identifiers: CAIP-2 (e.g. ``eip155:8453`` for Base mainnet).

See ADR-2026-03-03 for the full x402 integration design.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging

import httpx

from validibot_mcp.config import get_settings

logger = logging.getLogger(__name__)
_X402_HTTP_CLIENT: httpx.AsyncClient | None = None
_X402_HTTP_LOCK = asyncio.Lock()


async def _get_x402_http_client() -> httpx.AsyncClient:
    """Return the shared facilitator client for x402 verification requests.

    Uses an asyncio lock so concurrent coroutines at startup don't race
    to create separate clients (which would orphan a connection pool).
    """
    global _X402_HTTP_CLIENT
    if _X402_HTTP_CLIENT is not None:
        return _X402_HTTP_CLIENT
    async with _X402_HTTP_LOCK:
        # Double-check after acquiring the lock.
        if _X402_HTTP_CLIENT is None:
            _X402_HTTP_CLIENT = httpx.AsyncClient(timeout=10.0)
        return _X402_HTTP_CLIENT


async def aclose_x402_http_client() -> None:
    """Close the shared x402 facilitator client during ASGI shutdown."""

    global _X402_HTTP_CLIENT

    client = _X402_HTTP_CLIENT
    _X402_HTTP_CLIENT = None
    if client is not None:
        await client.aclose()


# ── Price conversion ───────────────────────────────────────────────��


def cents_to_usdc_atomic(cents: int) -> int:
    """Convert USD cents to USDC atomic units.

    USDC has 6 decimal places: 1 USDC = 1,000,000 atomic units.
    1 US cent = 10,000 atomic units.

    Examples:
        >>> cents_to_usdc_atomic(5)    # $0.05
        50000
        >>> cents_to_usdc_atomic(100)  # $1.00
        1000000
    """
    return cents * 10_000


# ── Payment requirements ────────────────────────────────────────────


def build_payment_requirements(
    *,
    price_cents: int,
    workflow_slug: str,
    workflow_description: str = "",
) -> dict:
    """Build the x402 v2 payment requirements object.

    This is the payload returned in the ``PAYMENT-REQUIRED`` response
    header when an agent calls a paid tool without a payment signature.
    Well-behaved x402 clients (Coinbase Agentkit, Cloudflare Agents SDK)
    parse this and handle payment automatically.

    Args:
        price_cents: Price in US cents (e.g. 10 = $0.10 USDC).
        workflow_slug: Slug of the workflow being paid for.
        workflow_description: Optional human-readable description.

    Returns:
        x402 v2 requirements dict, ready to be base64-encoded for the
        ``PAYMENT-REQUIRED`` response header.
    """
    settings = get_settings()
    usdc_atomic = cents_to_usdc_atomic(price_cents)

    description = f"Validibot validation: {workflow_slug}"
    if workflow_description:
        description = f"{description} — {workflow_description[:100]}"

    return {
        "x402Version": 2,
        "accepts": [
            {
                "scheme": "exact",
                "network": settings.x402_network,
                "maxAmountRequired": str(usdc_atomic),
                "asset": settings.x402_asset,
                "payTo": settings.x402_pay_to_address,
                "description": description,
            },
        ],
    }


def encode_requirements_header(requirements: dict) -> str:
    """Base64-encode a requirements dict for the PAYMENT-REQUIRED header.

    x402 v2 specifies that the ``PAYMENT-REQUIRED`` header value is a
    base64-encoded JSON string.
    """
    json_bytes = json.dumps(requirements, separators=(",", ":")).encode()
    return base64.b64encode(json_bytes).decode("ascii")


# ── Payment verification ───────────────────────────────────────────


async def verify_payment(
    payment_signature: str,
    requirements: dict,
) -> tuple[bool, str | None, str | None]:
    """Verify an x402 payment signature via the Coinbase CDP facilitator.

    Calls the facilitator's ``/verify`` endpoint with the agent's
    ``PAYMENT-SIGNATURE`` value and the requirements we generated.
    The facilitator checks:
    - The signature is cryptographically valid
    - The payment was broadcast on the correct network
    - The asset and payTo address match our requirements
    - The amount meets or exceeds ``maxAmountRequired``

    IMPORTANT: The facilitator does NOT guarantee replay protection.
    x402 receipts are proofs of immutable on-chain state — the same
    receipt may return ``isValid=True`` on repeated calls. Replay
    protection is handled by the ``X402Payment.txhash`` unique constraint
    in Django, not here.

    Args:
        payment_signature: The raw ``PAYMENT-SIGNATURE`` header value
            from the agent (base64-encoded signed payment payload).
        requirements: The requirements dict from ``build_payment_requirements()``.

    Returns:
        Tuple of ``(is_valid, txhash_or_none, wallet_address_or_none)``.
        On failure, returns ``(False, None, None)`` — fail closed.
    """
    settings = get_settings()

    try:
        http = await _get_x402_http_client()
        response = await http.post(
            f"{settings.x402_facilitator_url}/verify",
            json={
                "payment": payment_signature,
                "paymentRequirements": requirements,
            },
        )

        if response.status_code != 200:
            logger.warning(
                "x402 facilitator returned %d: %s",
                response.status_code,
                response.text[:200],
            )
            return (False, None, None)

        data = response.json()
        is_valid = data.get("isValid", False)
        txhash = data.get("txHash") or data.get("transactionHash")
        wallet = data.get("from") or data.get("senderAddress")

        if not is_valid:
            logger.info("x402 payment verification failed: %s", data)
            return (False, None, None)

        if not txhash:
            logger.warning("x402 facilitator returned isValid=True but no txHash")
            return (False, None, None)

        return (True, txhash, wallet)

    except httpx.TimeoutException:
        logger.warning("x402 facilitator timed out")
        return (False, None, None)
    except Exception:
        logger.exception("x402 facilitator verification failed unexpectedly")
        return (False, None, None)
