"""
Custom (user-defined) validator.

Custom validators are container-based validators where the Docker image is
defined by the user rather than being a built-in system validator. The
container is specified via the ``CustomValidator`` model (linked 1:1 to
``Validator``) and runs through the same ExecutionBackend infrastructure
as EnergyPlus and FMU validators.

## Output Envelope Structure

Custom validator containers are expected to produce a standard output
envelope (from validibot_shared). The exact structure of ``outputs``
depends on the container implementation — unlike EnergyPlus or FMU,
there is no fixed schema for the output signals.

Signal extraction is handled generically: if the envelope has an
``outputs`` attribute with a ``signals`` dict, those are used directly.
"""

from __future__ import annotations

import logging
from typing import Any

from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.advanced import AdvancedValidator
from validibot.validations.validators.base.registry import register_validator

logger = logging.getLogger(__name__)


@register_validator(ValidationType.CUSTOM_VALIDATOR)
class CustomValidator(AdvancedValidator):
    """
    User-defined container-based validator.

    Custom validators dispatch submissions to a user-specified Docker
    container via the ExecutionBackend. The container image and
    configuration are stored on the linked ``CustomValidator`` model.

    This class only needs to implement ``extract_output_signals()`` —
    the shared validate/post_execute_validate lifecycle is handled by
    ``AdvancedValidator``.
    """

    @property
    def validator_display_name(self) -> str:
        return "Custom"

    @classmethod
    def extract_output_signals(
        cls,
        output_envelope: Any,
    ) -> dict[str, Any] | None:
        """
        Extract output signals from a custom validator envelope.

        Custom containers are expected to place their output signals in
        ``outputs.signals`` as a flat dict. If the container uses a
        different structure, this method should be extended or the
        container should conform to the convention.

        Args:
            output_envelope: Output envelope from the custom container.

        Returns:
            Dict of signal name to value, or None if extraction fails.
        """
        try:
            outputs = getattr(output_envelope, "outputs", None)
            if not outputs:
                return None

            signals = getattr(outputs, "signals", None)
            if signals is None:
                return None

            # Pydantic model → dict
            if hasattr(signals, "model_dump"):
                return {
                    k: v
                    for k, v in signals.model_dump(mode="json").items()
                    if v is not None
                }

            if isinstance(signals, dict):
                return {k: v for k, v in signals.items() if v is not None}
        except Exception:
            logger.debug(
                "Could not extract signals from custom validator envelope",
            )

        return None
