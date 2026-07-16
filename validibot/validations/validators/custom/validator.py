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
there is no fixed schema for the output values.

Output-value extraction is handled generically: if the envelope has an
``outputs`` attribute with an ``output_values`` dict, those are used directly.
"""

from __future__ import annotations

import logging
from typing import Any

from validibot.validations.validators.base.advanced import AdvancedValidator

logger = logging.getLogger(__name__)


class CustomValidator(AdvancedValidator):
    """
    User-defined container-based validator.

    Custom validators dispatch submissions to a user-specified Docker
    container via the ExecutionBackend. The container image and
    configuration are stored on the linked ``CustomValidator`` model.

    This class only needs to implement ``extract_output_values()`` —
    the shared validate/post_execute_validate lifecycle is handled by
    ``AdvancedValidator``.
    """

    @property
    def validator_display_name(self) -> str:
        return "Custom"

    def extract_output_values(
        self,
        output_envelope: Any,
    ) -> dict[str, Any] | None:
        """
        Extract output values from a custom validator envelope.

        Custom containers are expected to place their output values in
        ``outputs.output_values`` as a flat dict. If the container uses a
        different structure, this method should be extended or the
        container should conform to the convention.

        Declared as an instance method to match the base contract — custom
        validators don't currently need run context for extraction, but
        consistency with EnergyPlus and FMU avoids surprises.

        Args:
            output_envelope: Output envelope from the custom container.

        Returns:
            Dict of output key to value, or None if extraction fails.
        """
        try:
            outputs = getattr(output_envelope, "outputs", None)
            if not outputs:
                return None

            output_values = getattr(outputs, "output_values", None)
            if output_values is None:
                return None

            # Pydantic model → dict
            if hasattr(output_values, "model_dump"):
                return {
                    k: v
                    for k, v in output_values.model_dump(mode="json").items()
                    if v is not None
                }

            if isinstance(output_values, dict):
                return {k: v for k, v in output_values.items() if v is not None}
        except Exception:
            logger.debug(
                "Could not extract output_values from custom validator envelope",
            )

        return None
