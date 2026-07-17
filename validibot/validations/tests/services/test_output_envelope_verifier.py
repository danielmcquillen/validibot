"""Tests for the trusted advanced-validator output-envelope verifier.

The local Docker, asynchronous callback, and GCP reconciliation paths must not
develop different ideas of valid output.  These tests pin the shared trust
boundary: trusted Django state chooses the parser, raw bytes are bounded before
parsing, and run plus validator identity must agree before output is accepted.
"""

from __future__ import annotations

import json

import pytest
from validibot_shared.energyplus.envelopes import EnergyPlusOutputEnvelope

from validibot.validations.services.output_envelope_verifier import (
    ExpectedOutputEnvelope,
)
from validibot.validations.services.output_envelope_verifier import (
    OutputEnvelopeVerificationError,
)
from validibot.validations.services.output_envelope_verifier import (
    output_envelope_sha256,
)
from validibot.validations.services.output_envelope_verifier import (
    parse_and_verify_output_envelope,
)
from validibot.validations.services.output_envelope_verifier import (
    verify_output_envelope,
)

RUN_ID = "run-123"
VALIDATOR_ID = "validator-456"


def _expected() -> ExpectedOutputEnvelope:
    """Return the trusted EnergyPlus identity used by verifier tests."""
    return ExpectedOutputEnvelope(
        run_id=RUN_ID,
        validator_id=VALIDATOR_ID,
        validator_type="ENERGYPLUS",
        envelope_class=EnergyPlusOutputEnvelope,
    )


def _payload(**overrides) -> bytes:
    """Build a minimal JSON output with optional top-level identity overrides."""
    data = {
        "schema_version": "validibot.output.v1",
        "run_id": RUN_ID,
        "validator": {
            "id": VALIDATOR_ID,
            "type": "ENERGYPLUS",
            "version": "1",
        },
        "status": "success",
        "timing": {},
        "outputs": {
            "energyplus_returncode": 0,
            "execution_seconds": 1.0,
            "invocation_mode": "cli",
        },
    }
    data.update(overrides)
    return json.dumps(data).encode("utf-8")


def test_valid_payload_is_parsed_with_the_trusted_class() -> None:
    """A matching payload must return the domain class selected by Django."""
    envelope = parse_and_verify_output_envelope(_payload(), expected=_expected())
    assert isinstance(envelope, EnergyPlusOutputEnvelope)
    assert envelope.outputs.energyplus_returncode == 0


def test_size_limit_is_checked_before_schema_parsing() -> None:
    """Oversized hostile bytes must fail without entering Pydantic parsing."""
    with pytest.raises(OutputEnvelopeVerificationError, match="byte limit") as exc:
        parse_and_verify_output_envelope(
            b"x" * 128,
            expected=_expected(),
            max_bytes=64,
        )
    assert exc.value.code == "output_too_large"


def test_invalid_schema_is_a_typed_verification_failure() -> None:
    """Malformed or wrong-schema JSON must remain a system contract failure."""
    with pytest.raises(OutputEnvelopeVerificationError) as exc:
        parse_and_verify_output_envelope(b"{}", expected=_expected())
    assert exc.value.code == "invalid_envelope"


def test_output_cannot_claim_another_run() -> None:
    """Stale output from another run must never satisfy this run's completion."""
    with pytest.raises(OutputEnvelopeVerificationError) as exc:
        parse_and_verify_output_envelope(
            _payload(run_id="another-run"),
            expected=_expected(),
        )
    assert exc.value.code == "run_mismatch"


def test_output_cannot_claim_another_validator_id() -> None:
    """A backend result must belong to the exact configured validator row."""
    validator = {
        "id": "another-validator",
        "type": "ENERGYPLUS",
        "version": "1",
    }
    with pytest.raises(OutputEnvelopeVerificationError) as exc:
        parse_and_verify_output_envelope(
            _payload(validator=validator),
            expected=_expected(),
        )
    assert exc.value.code == "validator_id_mismatch"


def test_output_cannot_change_its_validator_type() -> None:
    """The document's declared type cannot override trusted parser selection."""
    validator = {
        "id": VALIDATOR_ID,
        "type": "FMU",
        "version": "1",
    }
    with pytest.raises(OutputEnvelopeVerificationError) as exc:
        parse_and_verify_output_envelope(
            _payload(validator=validator),
            expected=_expected(),
        )
    assert exc.value.code == "validator_type_mismatch"


def test_parsed_callback_envelope_uses_the_same_identity_checks() -> None:
    """Callback downloads must pass the same verifier as local raw bytes."""
    envelope = EnergyPlusOutputEnvelope.model_validate_json(_payload())
    assert verify_output_envelope(envelope, expected=_expected()) is envelope


def test_verified_output_has_a_stable_canonical_digest() -> None:
    """Attempt evidence must hash canonical output, not provider JSON spacing."""
    compact = EnergyPlusOutputEnvelope.model_validate_json(_payload())
    pretty_payload = json.dumps(json.loads(_payload()), indent=4).encode("utf-8")
    pretty = EnergyPlusOutputEnvelope.model_validate_json(pretty_payload)
    assert output_envelope_sha256(compact) == output_envelope_sha256(pretty)
