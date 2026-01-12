"""
EnergyPlus validation engine powered by Cloud Run Jobs.

This engine forwards incoming EnergyPlus submissions (epJSON or IDF) to
Cloud Run Jobs and receives results via callbacks.

## Validation Flow

1. Engine receives validator, submission, ruleset from workflow execution
2. run_context is set by the handler with validation_run and workflow_step
3. If Cloud Run Jobs is configured, launches async job via launcher
4. Returns pending ValidationResult
5. Cloud Run Job executes and writes EnergyPlusOutputEnvelope to GCS
6. Job POSTs callback to Django with result_uri
7. Callback downloads envelope and evaluates output-stage assertions

## Output Envelope Structure

The EnergyPlus Cloud Run Job produces an `EnergyPlusOutputEnvelope` (from
vb_shared.energyplus.envelopes) containing:

- outputs.metrics: EnergyPlusSimulationMetrics with fields like:
  - site_eui_kwh_m2: Site energy use intensity
  - site_electricity_kwh: Total electricity consumption
  - site_natural_gas_kwh: Total gas consumption
  - etc. (see vb_shared/energyplus/models.py)

These metrics are extracted via `extract_output_signals()` for use in
output-stage CEL assertions (e.g., "site_eui_kwh_m2 < 100").

If Cloud Run Jobs is not configured (local dev), returns not-implemented error.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any

from django.conf import settings
from django.utils.translation import gettext as _

from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.engines.base import BaseValidatorEngine
from validibot.validations.engines.base import ValidationIssue
from validibot.validations.engines.base import ValidationResult
from validibot.validations.engines.registry import register_engine

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.submissions.models import Submission
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Validator


@register_engine(ValidationType.ENERGYPLUS)
class EnergyPlusValidationEngine(BaseValidatorEngine):
    """
    Run submitted epJSON through Cloud Run Jobs.

    This engine triggers async Cloud Run Jobs and returns pending results.
    The ValidationRun is updated via callback when the job completes.
    """

    @classmethod
    def extract_output_signals(cls, output_envelope: Any) -> dict[str, Any] | None:
        """
        Extract simulation metrics from an EnergyPlus output envelope.

        EnergyPlus envelopes (EnergyPlusOutputEnvelope from vb_shared) store
        metrics in outputs.metrics as an EnergyPlusSimulationMetrics Pydantic
        model. Fields include site_eui_kwh_m2, site_electricity_kwh, etc.

        Args:
            output_envelope: EnergyPlusOutputEnvelope from the Cloud Run Job.

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

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset | None,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """
        Validate an EnergyPlus submission.

        Launches a Cloud Run Job asynchronously and returns a pending result.

        Args:
            validator: EnergyPlus validator instance
            submission: Submission with IDF/epJSON content
            ruleset: Ruleset with weather_file metadata
            run_context: Required execution context with validation_run and step

        Returns:
            ValidationResult with passed=None (pending) if Cloud Run Jobs configured,
            or passed=False (error) if not configured or missing context.
        """
        # Store run_context on instance for CEL evaluation methods
        self.run_context = run_context

        # Validate that run_context is properly set
        run = run_context.validation_run if run_context else None
        step = run_context.step if run_context else None

        if not run or not step:
            logger.error(
                "EnergyPlus engine requires run_context to be set with "
                "validation_run and workflow_step"
            )
            issues = [
                ValidationIssue(
                    path="",
                    message=_(
                        "EnergyPlus validation requires workflow context. "
                        "Ensure the engine is called via the workflow handler.",
                    ),
                    severity=Severity.ERROR,
                ),
            ]
            return ValidationResult(
                passed=False,
                issues=issues,
                stats={"implementation_status": "Missing run_context"},
            )

        # Check if Cloud Run Jobs is configured
        if not settings.GCS_VALIDATION_BUCKET or not settings.GCS_ENERGYPLUS_JOB_NAME:
            logger.warning(
                "Cloud Run Jobs not configured - returning not-implemented error"
            )
            issues = [
                ValidationIssue(
                    path="",
                    message=_(
                        "EnergyPlus Cloud Run Jobs not configured. "
                        "Set GCS_VALIDATION_BUCKET and GCS_ENERGYPLUS_JOB_NAME "
                        "in production settings.",
                    ),
                    severity=Severity.ERROR,
                ),
            ]
            return ValidationResult(
                passed=False,
                issues=issues,
                stats={"implementation_status": "Not configured"},
            )

        # Import here to avoid circular dependency
        from validibot.validations.services.cloud_run.launcher import (
            launch_energyplus_validation,
        )

        # Launch Cloud Run Job asynchronously
        return launch_energyplus_validation(
            run=run,
            validator=validator,
            submission=submission,
            ruleset=ruleset,
            step=step,
        )
