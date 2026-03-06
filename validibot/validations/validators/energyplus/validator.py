"""
EnergyPlus validator.

This validator forwards incoming EnergyPlus submissions (epJSON or IDF) to
container-based validator jobs via the ExecutionBackend abstraction. This works
across different deployment targets:

- Docker Compose: Docker containers via local socket (synchronous)
- GCP: Cloud Run Jobs (async with callbacks)
- AWS: AWS Batch (future)

## Preprocessing

When a workflow step uses a parameterized IDF template, the submitter uploads
a JSON dict of variable values instead of a complete IDF.  The
``preprocess_submission()`` override delegates to ``energyplus.preprocessing``
to resolve the template into a full IDF **before** backend dispatch.  After
preprocessing, the submission looks identical to a direct-IDF upload —
execution backends never need to know templates exist.

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
from typing import TYPE_CHECKING
from typing import Any

from validibot.validations.constants import ValidationType
from validibot.validations.validators.base.advanced import AdvancedValidator
from validibot.validations.validators.base.registry import register_validator

if TYPE_CHECKING:
    from validibot.submissions.models import Submission
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


@register_validator(ValidationType.ENERGYPLUS)
class EnergyPlusValidator(AdvancedValidator):
    """
    EnergyPlus simulation validator.

    Dispatches EnergyPlus submissions (IDF/epJSON) to container-based validator
    jobs via the ExecutionBackend. The actual simulation runs inside a Docker
    container defined in the ``validibot-validators`` repository.

    Overrides:

    - ``preprocess_submission()`` — resolves parameterized IDF templates into
      a complete IDF before backend dispatch (delegates to
      ``energyplus.preprocessing``).
    - ``extract_output_signals()`` — extracts simulation metrics for assertions.

    The shared validate/post_execute_validate lifecycle is handled by
    ``AdvancedValidator``.
    """

    @property
    def validator_display_name(self) -> str:
        return "EnergyPlus"

    def preprocess_submission(
        self,
        *,
        step: WorkflowStep,
        submission: Submission,
    ) -> dict[str, object]:
        """Resolve parameterized IDF templates before execution dispatch.

        If the step has a ``MODEL_TEMPLATE`` resource, the submission is
        treated as a JSON dict of variable values.  This method delegates
        to ``energyplus.preprocessing`` which merges values with author
        defaults, validates constraints, substitutes ``$VARIABLE``
        placeholders, and overwrites ``submission.content`` with the
        resolved IDF.

        For direct-IDF submissions (no template resource), this is a no-op.

        Returns:
            Metadata dict with ``template_parameters_used`` and
            ``template_warnings`` (merged into ``step_run.output`` by the
            base class), or empty dict for direct-mode submissions.
        """
        from validibot.validations.validators.energyplus.preprocessing import (
            preprocess_energyplus_submission,
        )

        result = preprocess_energyplus_submission(step=step, submission=submission)
        return result.template_metadata

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
