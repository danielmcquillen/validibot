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

from validibot.validations.constants import Severity
from validibot.validations.validators.base.advanced import AdvancedValidator

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.submissions.models import Submission
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Validator
    from validibot.validations.validators.base.base import ValidationResult
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


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

    def _should_filter_warnings(self, run_context: RunContext | None) -> bool:
        """Check whether simulation warnings should be suppressed.

        Reads ``show_energyplus_warnings`` from the step config. When False,
        non-ERROR issues from the EnergyPlus ``.err`` file should be stripped
        before they are persisted as findings.
        """
        step = run_context.step if run_context else None
        if not step:
            return False
        config = step.config or {}
        return not config.get("show_energyplus_warnings", True)

    def _filter_issues(
        self,
        result: ValidationResult,
        run_context: RunContext | None,
    ) -> ValidationResult:
        """Remove non-ERROR issues from result when warnings are suppressed."""
        if self._should_filter_warnings(run_context):
            result.issues = [
                issue for issue in result.issues if issue.severity == Severity.ERROR
            ]
        return result

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset | None,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """Dispatch to container, filtering warnings from sync results.

        For sync backends (Docker Compose), ``validate()`` returns with
        envelope messages already extracted into ``result.issues``. These
        issues are persisted by the processor *before* ``post_execute_validate()``
        runs.  We must filter here to prevent warnings from being saved as
        findings when ``show_energyplus_warnings`` is disabled.
        """
        result = super().validate(validator, submission, ruleset, run_context)
        return self._filter_issues(result, run_context)

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

    def post_execute_validate(
        self,
        output_envelope: Any,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """Process container output, optionally filtering simulation warnings.

        Delegates to the base ``AdvancedValidator.post_execute_validate()``
        for envelope processing, signal extraction, and assertion evaluation.
        Then applies warning filtering if ``show_energyplus_warnings`` is
        disabled in the step config.

        This method handles the async callback path and the output-stage
        extraction.  The sync path's initial extraction is filtered in
        ``validate()`` above.
        """
        result = super().post_execute_validate(output_envelope, run_context)
        return self._filter_issues(result, run_context)

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
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning(
                "Could not extract signals from EnergyPlus envelope: %s",
                exc,
                exc_info=True,
            )

        return None
