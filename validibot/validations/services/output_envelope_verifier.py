"""Trusted parsing and identity verification for validator output envelopes.

Advanced-validator output is untrusted container data.  Every completion path
must therefore select the Pydantic class from trusted validator configuration
and apply the same run/validator identity checks before processing findings or
marking an execution attempt complete.  Keeping those rules here prevents the
local Docker, callback, and reconciliation paths from drifting apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import cast

from pydantic import BaseModel
from pydantic import ValidationError
from validibot_shared.canonicalization import sha256_hex_for_model

if TYPE_CHECKING:
    from validibot_shared.validations.envelopes import ValidationOutputEnvelope

    from validibot.validations.models import ValidationRun
    from validibot.validations.models import Validator


class OutputEnvelopeVerificationError(ValueError):
    """An output envelope failed a trusted schema or identity requirement."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class ExpectedOutputEnvelope:
    """Trusted identity and schema selected before reading validator output."""

    run_id: str
    validator_id: str
    validator_type: str
    envelope_class: type[ValidationOutputEnvelope]


def build_expected_output_envelope(
    *,
    run: ValidationRun,
    validator: Validator,
) -> ExpectedOutputEnvelope:
    """Build expected output identity exclusively from trusted Django state."""
    from validibot.validations.validators.base.config import get_output_envelope_class

    envelope_class = get_output_envelope_class(validator.validation_type)
    if envelope_class is None:
        raise OutputEnvelopeVerificationError(
            "missing_envelope_class",
            "No output envelope class is registered for the expected validator.",
        )
    return ExpectedOutputEnvelope(
        run_id=str(run.pk),
        validator_id=str(validator.pk),
        validator_type=_normalise_validator_type(validator.validation_type),
        envelope_class=cast("type[ValidationOutputEnvelope]", envelope_class),
    )


def parse_and_verify_output_envelope(
    payload: bytes,
    *,
    expected: ExpectedOutputEnvelope,
    max_bytes: int | None = None,
) -> ValidationOutputEnvelope:
    """Parse bounded bytes with the trusted class, then verify their identity."""
    if max_bytes is not None and len(payload) > max_bytes:
        raise OutputEnvelopeVerificationError(
            "output_too_large",
            "Output envelope exceeds the configured byte limit.",
        )
    try:
        envelope = expected.envelope_class.model_validate_json(payload)
    except (ValidationError, ValueError, UnicodeDecodeError) as exc:
        raise OutputEnvelopeVerificationError(
            "invalid_envelope",
            "Output envelope does not match the expected schema.",
        ) from exc
    return verify_output_envelope(envelope, expected=expected)


def verify_output_envelope(
    envelope: ValidationOutputEnvelope,
    *,
    expected: ExpectedOutputEnvelope,
) -> ValidationOutputEnvelope:
    """Verify a parsed output envelope against trusted run and validator state."""
    actual_run_id = str(getattr(envelope, "run_id", ""))
    if actual_run_id != expected.run_id:
        raise OutputEnvelopeVerificationError(
            "run_mismatch",
            "Run mismatch in output envelope.",
        )

    validator_info = getattr(envelope, "validator", None)
    actual_validator_id = str(getattr(validator_info, "id", ""))
    if actual_validator_id != expected.validator_id:
        raise OutputEnvelopeVerificationError(
            "validator_id_mismatch",
            "Validator mismatch in output envelope.",
        )

    actual_validator_type = _normalise_validator_type(
        getattr(validator_info, "type", ""),
    )
    if actual_validator_type != expected.validator_type:
        raise OutputEnvelopeVerificationError(
            "validator_type_mismatch",
            "Validator type mismatch in output envelope.",
        )

    return envelope


def output_envelope_sha256(envelope: BaseModel) -> str:
    """Return the canonical SHA-256 recorded for a verified output envelope."""
    return sha256_hex_for_model(envelope)


def _normalise_validator_type(value: object) -> str:
    """Return a stable comparison value for Django, Enum, and string types."""
    enum_value = getattr(value, "value", value)
    return str(enum_value).upper()


__all__ = [
    "ExpectedOutputEnvelope",
    "OutputEnvelopeVerificationError",
    "build_expected_output_envelope",
    "output_envelope_sha256",
    "parse_and_verify_output_envelope",
    "verify_output_envelope",
]
