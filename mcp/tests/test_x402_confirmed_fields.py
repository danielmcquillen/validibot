"""
Regression tests for the x402 "confirmed-fields" hardening.

These tests cover the MEDIUM-severity fix where ``verify_payment`` used to
return only ``(is_valid, txhash, wallet)`` and discard the facilitator-
confirmed ``payTo`` / ``network`` / ``asset`` / ``amount``. The validate tool
then forwarded this server's OWN config values to Django, making any
downstream pay-to comparison tautological: a misconfigured or compromised
facilitator could settle a payment to a different wallet, chain, or asset and
every downstream check would still pass because Django was handed an echo of
local config rather than what actually settled.

The fix has two halves, one per assigned source file:

1. ``x402.verify_payment`` now returns a :class:`VerifiedPayment` carrying the
   facilitator-CONFIRMED settlement dimensions extracted from the ``/verify``
   response (or ``None`` on any failure — fail closed).
2. ``tools.validate._confirm_or_reject`` asserts each confirmed field equals
   this server's configured expectation, rejecting (fail closed) on mismatch
   and falling back with a logged warning when the facilitator omits a field.

Why these tests matter: they pin the security-relevant behaviour — that a
facilitator-confirmed pay-to address which disagrees with local config is
REJECTED rather than silently accepted — so a future refactor cannot quietly
reintroduce the tautological-comparison bug.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from validibot_mcp.errors import PaymentInvalidError
from validibot_mcp.tools.validate import _confirm_or_reject
from validibot_mcp.x402 import build_payment_requirements, verify_payment

FACILITATOR_URL = "https://api.cdp.coinbase.com/platform/v2/x402"
SAMPLE_TXHASH = "0x" + "cd" * 32
EXPECTED_PAY_TO = "0xRECEIVER_WALLET"


class TestVerifyPaymentReturnsConfirmedFields:
    """``verify_payment`` must surface the facilitator-confirmed dimensions.

    The whole point of the fix is that downstream code can compare against what
    the facilitator confirmed settled on-chain, not against local config. If
    these fields were not returned, the MCP layer would have nothing to assert
    on and would be forced back to forwarding its own config (the original
    bug).
    """

    @pytest.fixture(autouse=True)
    def _setup_settings(self, monkeypatch):
        """Point the facilitator URL and pay-to at known test values.

        Clearing the settings cache ensures the patched env vars take effect
        for the property-backed x402 settings rather than a stale cached
        instance from another test module.
        """
        monkeypatch.setenv("VALIDIBOT_X402_FACILITATOR_URL", FACILITATOR_URL)
        monkeypatch.setenv("VALIDIBOT_X402_PAY_TO_ADDRESS", EXPECTED_PAY_TO)
        from validibot_mcp.config import get_settings

        get_settings.cache_clear()

    @respx.mock
    async def test_confirmed_pay_to_is_extracted_not_echoed(self):
        """A valid receipt must return the facilitator's confirmed pay-to.

        This is the load-bearing assertion: the facilitator response's
        ``payTo`` is what we forward to Django, NOT this server's configured
        address. Here we make the facilitator confirm a DIFFERENT address than
        our config so the test fails loudly if the code ever reverts to
        echoing config.
        """
        facilitator_pay_to = "0xFACILITATOR_CONFIRMED_ADDR"
        respx.post(f"{FACILITATOR_URL}/verify").mock(
            return_value=httpx.Response(
                200,
                json={
                    "isValid": True,
                    "txHash": SAMPLE_TXHASH,
                    "from": "0xSENDER",
                    "payTo": facilitator_pay_to,
                    "network": "eip155:8453",
                    "asset": "0xUSDC",
                    "amount": "100000",
                },
            ),
        )
        requirements = build_payment_requirements(
            price_cents=10,
            workflow_slug="test",
        )

        verified = await verify_payment("sig", requirements)

        assert verified is not None
        assert verified.txhash == SAMPLE_TXHASH
        assert verified.pay_to == facilitator_pay_to
        assert verified.network == "eip155:8453"
        assert verified.asset == "0xUSDC"
        assert verified.amount == "100000"

    @respx.mock
    async def test_omitted_fields_are_none_not_invented(self):
        """When the facilitator omits confirmed fields they must be ``None``.

        A sparse-but-valid facilitator response must not be silently filled in
        at this layer — the caller is responsible for deciding whether to fall
        back. Returning ``None`` (rather than a guessed value) keeps that
        decision explicit and auditable.
        """
        respx.post(f"{FACILITATOR_URL}/verify").mock(
            return_value=httpx.Response(
                200,
                json={"isValid": True, "txHash": SAMPLE_TXHASH, "from": "0xS"},
            ),
        )
        requirements = build_payment_requirements(
            price_cents=10,
            workflow_slug="test",
        )

        verified = await verify_payment("sig", requirements)

        assert verified is not None
        assert verified.pay_to is None
        assert verified.network is None
        assert verified.asset is None
        assert verified.amount is None


class TestConfirmOrReject:
    """``_confirm_or_reject`` is the fail-closed assertion gate.

    These tests pin the three branches that matter for the fix: a matching
    confirmed value is forwarded, a mismatching one is rejected, and an omitted
    one falls back to config (so we never reject a sparse-but-valid receipt).
    """

    def test_mismatch_rejects_fail_closed(self):
        """A confirmed value that disagrees with config must raise.

        This is the core security guarantee: if the facilitator confirms a
        pay-to address different from what this server expects, the run is
        refused before it is ever created — closing the tautological-comparison
        gap that the original code left open.
        """
        with pytest.raises(PaymentInvalidError):
            _confirm_or_reject(
                confirmed="0xATTACKER_WALLET",
                expected=EXPECTED_PAY_TO,
                field="pay_to",
                case_insensitive=True,
            )

    def test_match_is_case_insensitive_for_addresses(self):
        """EVM addresses compare case-insensitively and the value is forwarded.

        EIP-55 mixed-case checksums encode the same underlying bytes, so a
        case difference between the facilitator's confirmation and our config
        must NOT be treated as a mismatch. The confirmed value (not config) is
        what gets returned for forwarding to Django.
        """
        result = _confirm_or_reject(
            confirmed=EXPECTED_PAY_TO.upper(),
            expected=EXPECTED_PAY_TO.lower(),
            field="pay_to",
            case_insensitive=True,
        )
        assert result == EXPECTED_PAY_TO.upper()

    def test_omitted_field_falls_back_to_expected(self):
        """A ``None`` confirmed value falls back to the configured expectation.

        When the facilitator omits a field we degrade gracefully rather than
        failing the whole payment — but we return the expected value so the
        downstream forward is still well-formed (the omission is logged by the
        production code as a weaker-verification signal).
        """
        result = _confirm_or_reject(
            confirmed=None,
            expected=EXPECTED_PAY_TO,
            field="pay_to",
            case_insensitive=True,
        )
        assert result == EXPECTED_PAY_TO
