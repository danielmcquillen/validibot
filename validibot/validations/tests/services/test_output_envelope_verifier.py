"""Tests for the trusted advanced-validator output-envelope verifier.

The local Docker, asynchronous callback, and GCP reconciliation paths must not
develop different ideas of valid output.  These tests pin the shared trust
boundary: trusted Django state chooses the parser, raw bytes are bounded before
parsing, and run plus validator identity must agree before output is accepted.
"""

from __future__ import annotations

import json

import pytest
from validibot_shared.canonicalization import sha256_hex_for_model
from validibot_shared.energyplus.envelopes import EnergyPlusOutputEnvelope
from validibot_shared.validations.envelopes import ATTEMPT_CONTRACT_VERSION
from validibot_shared.validations.envelopes import SupportedMimeType
from validibot_shared.validations.envelopes import ValidationInputEnvelope
from validibot_shared.validations.envelopes import ValidatorType

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
STEP_RUN_ID = "step-run-789"
ATTEMPT_ID = "attempt-012"
INPUT_SHA256 = "a" * 64
OUTPUT_URI = "gs://bucket/runs/run-123/output.json"


def _expected() -> ExpectedOutputEnvelope:
    """Return the trusted EnergyPlus identity used by verifier tests."""
    return ExpectedOutputEnvelope(
        run_id=RUN_ID,
        validator_id=VALIDATOR_ID,
        validator_type="ENERGYPLUS",
        step_run_id=STEP_RUN_ID,
        execution_attempt_id=ATTEMPT_ID,
        attempt_contract_version="validibot.attempt.v1",
        input_envelope_sha256=INPUT_SHA256,
        output_uri=OUTPUT_URI,
        envelope_class=EnergyPlusOutputEnvelope,
    )


def _payload(**overrides) -> bytes:
    """Build a minimal JSON output with optional top-level identity overrides."""
    data = {
        "schema_version": "validibot.output.v1",
        "run_id": RUN_ID,
        "step_run_id": STEP_RUN_ID,
        "execution_attempt_id": ATTEMPT_ID,
        "attempt_contract_version": "validibot.attempt.v1",
        "input_envelope_sha256": INPUT_SHA256,
        "output_uri": OUTPUT_URI,
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


@pytest.mark.parametrize(
    ("field_name", "wrong_value", "expected_code"),
    [
        ("step_run_id", "another-step", "step_run_mismatch"),
        (
            "execution_attempt_id",
            "another-attempt",
            "execution_attempt_mismatch",
        ),
        (
            "attempt_contract_version",
            "validibot.attempt.v2",
            "invalid_envelope",
        ),
        (
            "input_envelope_sha256",
            "b" * 64,
            "input_envelope_digest_mismatch",
        ),
        ("output_uri", "gs://bucket/other/output.json", "output_uri_mismatch"),
    ],
)
def test_output_must_echo_every_committed_attempt_identity(
    field_name: str,
    wrong_value: str,
    expected_code: str,
) -> None:
    """A stale, redirected, or differently-versioned output must fail closed."""
    with pytest.raises(OutputEnvelopeVerificationError) as exc:
        parse_and_verify_output_envelope(
            _payload(**{field_name: wrong_value}),
            expected=_expected(),
        )
    assert exc.value.code == expected_code


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


def test_shared_attempt_fixture_digest_matches_the_backend_contract() -> None:
    """Django dispatch hashing must match the literal pinned in every repo."""
    envelope = ValidationInputEnvelope(
        run_id="run-fixture",
        validator={
            "id": "validator-fixture",
            "type": ValidatorType.FMU,
            "version": "1",
        },
        org={"id": "org-fixture", "name": "Fixture Org"},
        workflow={
            "id": "workflow-fixture",
            "step_id": "step-fixture",
            "step_name": "Fixture Step",
        },
        input_files=[
            {
                "name": "model.fmu",
                "mime_type": SupportedMimeType.FMU,
                "role": "fmu",
                "port_key": "fmu_model",
                "uri": "gs://fixture/runs/run-fixture/model.fmu",
                "size_bytes": 12,
                "sha256": "1" * 64,
                "storage_version": "1700000000000000",
            },
        ],
        inputs={"alpha": 1},
        context={
            "execution_attempt_id": "attempt-fixture",
            "step_run_id": "step-run-fixture",
            "attempt_contract_version": ATTEMPT_CONTRACT_VERSION,
            "expected_output_uri": "gs://fixture/runs/run-fixture/output.json",
            "execution_bundle_uri": "gs://fixture/runs/run-fixture/",
            "skip_callback": True,
        },
    )

    assert sha256_hex_for_model(envelope) == (
        "e17c5dae05c58f4d6034806e3f5e7a7602013d03f27ec811a97a9fc49f9d88d5"
    )
