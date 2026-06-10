"""
Tests for the x402 payment protocol module.

These tests verify:
- Price conversion (USD cents → USDC atomic units)
- Payment requirements building (x402 v2 format)
- Requirements header encoding (base64 for PAYMENT-REQUIRED header)
- Payment verification via the Coinbase facilitator (mocked)

The facilitator is mocked via respx — no real HTTP calls are made.
The tests verify that the module correctly handles valid payments,
invalid payments, facilitator timeouts, and malformed responses.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from validibot_mcp.x402 import (
    VerifiedPayment,
    build_payment_requirements,
    cents_to_usdc_atomic,
    encode_requirements_header,
    verify_payment,
)

# ── Price conversion ────────────────────────────────────────────────


class TestCentsToUsdcAtomic:
    """Verify the cents-to-USDC-atomic-units conversion.

    USDC has 6 decimal places: 1 USDC = 1,000,000 atomic units.
    1 US cent = 10,000 atomic units.
    """

    def test_five_cents(self):
        """$0.05 = 50,000 atomic units."""
        assert cents_to_usdc_atomic(5) == 50_000

    def test_one_dollar(self):
        """$1.00 = 1,000,000 atomic units."""
        assert cents_to_usdc_atomic(100) == 1_000_000

    def test_ten_cents(self):
        """$0.10 = 100,000 atomic units."""
        assert cents_to_usdc_atomic(10) == 100_000

    def test_zero(self):
        """$0.00 = 0 atomic units."""
        assert cents_to_usdc_atomic(0) == 0


# ── Payment requirements ────────────────────────────────────────────


class TestBuildPaymentRequirements:
    """Verify that requirements objects conform to x402 v2 format.

    The requirements dict is returned in the PAYMENT-REQUIRED response
    so agents know how to pay. It must include the correct network,
    asset, amount, and pay-to address.
    """

    def test_x402_version_is_2(self, monkeypatch):
        """Requirements should use x402 v2 format."""
        monkeypatch.setenv("VALIDIBOT_X402_PAY_TO_ADDRESS", "0xTEST")
        from validibot_mcp.config import get_settings

        get_settings.cache_clear()

        reqs = build_payment_requirements(
            price_cents=10,
            workflow_slug="energy-check",
        )
        assert reqs["x402Version"] == 2

    def test_amount_converted_correctly(self, monkeypatch):
        """The maxAmountRequired should be the USDC atomic representation
        of the price in cents."""
        monkeypatch.setenv("VALIDIBOT_X402_PAY_TO_ADDRESS", "0xTEST")
        from validibot_mcp.config import get_settings

        get_settings.cache_clear()

        reqs = build_payment_requirements(
            price_cents=50,
            workflow_slug="energy-check",
        )
        assert reqs["accepts"][0]["maxAmountRequired"] == "500000"

    def test_includes_workflow_slug_in_description(self, monkeypatch):
        """The description should include the workflow slug so agents
        know what they're paying for."""
        monkeypatch.setenv("VALIDIBOT_X402_PAY_TO_ADDRESS", "0xTEST")
        from validibot_mcp.config import get_settings

        get_settings.cache_clear()

        reqs = build_payment_requirements(
            price_cents=10,
            workflow_slug="ashrae-901",
            workflow_description="ASHRAE 90.1 compliance check",
        )
        assert "ashrae-901" in reqs["accepts"][0]["description"]

    def test_uses_configured_network(self, monkeypatch):
        """The network should come from settings, defaulting to Base mainnet."""
        monkeypatch.setenv("VALIDIBOT_X402_PAY_TO_ADDRESS", "0xTEST")
        monkeypatch.setenv("VALIDIBOT_X402_NETWORK", "eip155:84532")
        from validibot_mcp.config import get_settings

        get_settings.cache_clear()

        reqs = build_payment_requirements(
            price_cents=10,
            workflow_slug="test",
        )
        assert reqs["accepts"][0]["network"] == "eip155:84532"


# ── Requirements encoding ───────────────────────────────────────────


class TestEncodeRequirementsHeader:
    """Verify that requirements are correctly base64-encoded for the
    PAYMENT-REQUIRED response header."""

    def test_roundtrip(self, monkeypatch):
        """Encoding then decoding should return the original requirements."""
        import base64

        monkeypatch.setenv("VALIDIBOT_X402_PAY_TO_ADDRESS", "0xTEST")
        from validibot_mcp.config import get_settings

        get_settings.cache_clear()

        reqs = build_payment_requirements(
            price_cents=10,
            workflow_slug="test",
        )
        encoded = encode_requirements_header(reqs)
        decoded = json.loads(base64.b64decode(encoded))
        assert decoded["x402Version"] == reqs["x402Version"]
        assert decoded["accepts"][0]["maxAmountRequired"] == "100000"


# ── Payment verification ───────────────────────────────────────────


SAMPLE_TXHASH = "0x" + "ab" * 32
SAMPLE_WALLET = "0xSENDER_WALLET"
FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"


class TestVerifyPayment:
    """Verify the facilitator call and response handling.

    The facilitator is mocked via respx. These tests verify that
    verify_payment correctly interprets the facilitator's response
    and fails closed on errors.
    """

    @pytest.fixture(autouse=True)
    def _setup_settings(self, monkeypatch):
        """Configure x402 settings for all tests in this class."""
        monkeypatch.setenv("VALIDIBOT_X402_FACILITATOR_URL", FACILITATOR_URL)
        monkeypatch.setenv("VALIDIBOT_X402_PAY_TO_ADDRESS", "0xTEST")
        from validibot_mcp.config import get_settings

        get_settings.cache_clear()

    @respx.mock
    async def test_valid_receipt_returns_verified_payment(self):
        """A facilitator-verified receipt yields a populated ``VerifiedPayment``.

        ``verify_payment`` now returns a ``VerifiedPayment`` on success (and
        ``None`` on any failure) rather than the old
        ``(is_valid, txhash, wallet)`` tuple. This is the happy path: a valid
        receipt must come back as a ``VerifiedPayment`` carrying the on-chain
        txhash and payer wallet, because the caller forwards those to Django to
        create the replay-protected run.
        """
        respx.post(f"{FACILITATOR_URL}/verify").mock(
            return_value=httpx.Response(
                200,
                json={
                    "isValid": True,
                    "txHash": SAMPLE_TXHASH,
                    "from": SAMPLE_WALLET,
                },
            ),
        )
        requirements = build_payment_requirements(
            price_cents=10,
            workflow_slug="test",
        )
        result = await verify_payment(
            "base64-payment-sig",
            requirements,
        )
        assert isinstance(result, VerifiedPayment)
        assert result.txhash == SAMPLE_TXHASH
        assert result.wallet == SAMPLE_WALLET

    @respx.mock
    async def test_invalid_receipt_returns_none(self):
        """An invalid receipt yields ``None`` (fail closed).

        This is the normal case for bad/expired/insufficient payments. Under
        the new contract a non-valid receipt is signalled by returning ``None``
        (no ``VerifiedPayment`` is produced), so the caller never creates a run
        for an unverified payment.
        """
        respx.post(f"{FACILITATOR_URL}/verify").mock(
            return_value=httpx.Response(
                200,
                json={"isValid": False},
            ),
        )
        requirements = build_payment_requirements(
            price_cents=10,
            workflow_slug="test",
        )
        result = await verify_payment(
            "bad-sig",
            requirements,
        )
        assert result is None

    @respx.mock
    async def test_facilitator_timeout_fails_closed(self):
        """If the facilitator times out, verification fails closed (``None``).

        We never assume a payment is valid on timeout — an exception from the
        facilitator call must produce ``None`` so no run is created.
        """
        respx.post(f"{FACILITATOR_URL}/verify").mock(
            side_effect=httpx.TimeoutException("Connection timed out"),
        )
        requirements = build_payment_requirements(
            price_cents=10,
            workflow_slug="test",
        )
        result = await verify_payment(
            "any-sig",
            requirements,
        )
        assert result is None

    @respx.mock
    async def test_facilitator_500_fails_closed(self):
        """If the facilitator returns a server error, fail closed (``None``).

        A non-200 facilitator response must never be treated as a valid
        payment, so verification returns ``None``.
        """
        respx.post(f"{FACILITATOR_URL}/verify").mock(
            return_value=httpx.Response(500, text="Internal Server Error"),
        )
        requirements = build_payment_requirements(
            price_cents=10,
            workflow_slug="test",
        )
        result = await verify_payment(
            "any-sig",
            requirements,
        )
        assert result is None

    @respx.mock
    async def test_valid_but_no_txhash_fails_closed(self):
        """``isValid=True`` but no ``txHash`` fails closed (``None``).

        Without a transaction hash there is no audit trail / replay key to
        record, so even a "valid" facilitator response must return ``None``
        rather than letting the run proceed.
        """
        respx.post(f"{FACILITATOR_URL}/verify").mock(
            return_value=httpx.Response(
                200,
                json={"isValid": True},
            ),
        )
        requirements = build_payment_requirements(
            price_cents=10,
            workflow_slug="test",
        )
        result = await verify_payment(
            "any-sig",
            requirements,
        )
        assert result is None
