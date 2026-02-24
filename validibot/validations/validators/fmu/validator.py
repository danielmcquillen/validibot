"""
FMU validator.

This validator forwards FMU submissions to container-based validator jobs via
the ExecutionBackend abstraction. The FMU is executed in a containerized
environment with the FMU runtime.

This works across different deployment targets:

- Docker Compose: Docker containers via local socket (synchronous)
- GCP: Cloud Run Jobs (async with callbacks)
- AWS: AWS Batch (future)

## Output Envelope Structure

The FMU validator container produces an ``FMUOutputEnvelope`` (from
validibot_shared.fmi.envelopes) containing:

- outputs.output_values: Dict keyed by catalog slug with simulation outputs
  - Each key is a catalog entry slug (e.g., "indoor_temp_c")
  - Values are the simulation outputs for that signal

These output values are extracted via ``extract_output_signals()`` for use in
output-stage assertions (e.g., "indoor_temp_c < 26").
"""

from __future__ import annotations

import logging
from typing import Any

from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.advanced import AdvancedValidator
from validibot.validations.validators.base.registry import register_validator

logger = logging.getLogger(__name__)


@register_validator(ValidationType.FMU)
class FMUValidator(AdvancedValidator):
    """
    FMU (Functional Mock-up Unit) validator.

    Dispatches FMU files to container-based validator jobs via the
    ExecutionBackend. The actual FMU execution runs inside a Docker
    container defined in the ``validibot-validators`` repository.

    This class only needs to implement ``extract_output_signals()`` — the
    shared validate/post_execute_validate lifecycle is handled by
    ``AdvancedValidator``.
    """

    @property
    def validator_display_name(self) -> str:
        return "FMU"

    @classmethod
    def extract_output_signals(cls, output_envelope: Any) -> dict[str, Any] | None:
        """
        Extract output values from an FMU output envelope.

        FMU envelopes (FMUOutputEnvelope from validibot_shared) store simulation
        outputs in outputs.output_values as a dict keyed by catalog slug.

        Args:
            output_envelope: FMUOutputEnvelope from the validator container.

        Returns:
            Dict of output values keyed by catalog slug. Returns None if
            extraction fails.
        """
        try:
            outputs = getattr(output_envelope, "outputs", None)
            if not outputs:
                return None

            output_values = getattr(outputs, "output_values", None)
            if not output_values:
                return None

            # Handle Pydantic model
            if hasattr(output_values, "model_dump"):
                return output_values.model_dump(mode="json")

            # Handle plain dict
            if isinstance(output_values, dict):
                return output_values
        except Exception:
            logger.debug("Could not extract assertion signals from FMU envelope")

        return None
