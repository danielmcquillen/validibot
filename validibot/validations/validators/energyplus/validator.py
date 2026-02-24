"""
EnergyPlus validator.

This validator forwards incoming EnergyPlus submissions (epJSON or IDF) to
container-based validator jobs via the ExecutionBackend abstraction. This works
across different deployment targets:

- Docker Compose: Docker containers via local socket (synchronous)
- GCP: Cloud Run Jobs (async with callbacks)
- AWS: AWS Batch (future)

## Output Envelope Structure

The EnergyPlus validator container produces an ``EnergyPlusOutputEnvelope``
(from validibot_shared.energyplus.envelopes) containing:

- outputs.metrics: EnergyPlusSimulationMetrics with fields like:
  - site_eui_kwh_m2: Site energy use intensity
  - site_electricity_kwh: Total electricity consumption
  - site_natural_gas_kwh: Total gas consumption
  - etc. (see validibot_shared/energyplus/models.py)

These metrics are extracted via ``extract_output_signals()`` for use in
output-stage assertions (e.g., "site_eui_kwh_m2 < 100").
"""

from __future__ import annotations

import logging
from typing import Any

from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.advanced import AdvancedValidator
from validibot.validations.validators.base.registry import register_validator

logger = logging.getLogger(__name__)


@register_validator(ValidationType.ENERGYPLUS)
class EnergyPlusValidator(AdvancedValidator):
    """
    EnergyPlus simulation validator.

    Dispatches EnergyPlus submissions (IDF/epJSON) to container-based validator
    jobs via the ExecutionBackend. The actual simulation runs inside a Docker
    container defined in the ``validibot-validators`` repository.

    This class only needs to implement ``extract_output_signals()`` — the
    shared validate/post_execute_validate lifecycle is handled by
    ``AdvancedValidator``.
    """

    @property
    def validator_display_name(self) -> str:
        return "EnergyPlus"

    @classmethod
    def extract_output_signals(cls, output_envelope: Any) -> dict[str, Any] | None:
        """
        Extract simulation metrics from an EnergyPlus output envelope.

        EnergyPlus envelopes (EnergyPlusOutputEnvelope from validibot_shared)
        store metrics in outputs.metrics as an EnergyPlusSimulationMetrics
        Pydantic model. Fields include site_eui_kwh_m2, site_electricity_kwh,
        etc.

        Args:
            output_envelope: EnergyPlusOutputEnvelope from the validator container.

        Returns:
            Dict of metrics keyed by field name (matching catalog slugs), with
            None values filtered out. Returns None if extraction fails.
        """
        try:
            outputs = getattr(output_envelope, "outputs", None)
            if not outputs:
                return None

            metrics = getattr(outputs, "metrics", None)
            if not metrics:
                return None

            # Pydantic model_dump converts to dict; filter None values
            if hasattr(metrics, "model_dump"):
                metrics_dict = metrics.model_dump(mode="json")
                return {k: v for k, v in metrics_dict.items() if v is not None}

            # Fallback if metrics is already a dict
            if isinstance(metrics, dict):
                return {k: v for k, v in metrics.items() if v is not None}
        except Exception:
            logger.debug("Could not extract assertion signals from EnergyPlus envelope")

        return None
