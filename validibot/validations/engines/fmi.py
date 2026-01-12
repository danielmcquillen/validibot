"""
FMI validation engine powered by Cloud Run Jobs.

This engine forwards FMU submissions to Cloud Run Jobs for execution and
translates the response into Validibot issues. The FMU is executed in a
containerized environment with the FMI runtime.

## Validation Flow

1. Engine receives validator, submission, ruleset from workflow execution
2. run_context is set by the handler with validation_run and workflow_step
3. If Cloud Run Jobs is configured, launches async job via launcher
4. Returns pending ValidationResult
5. Cloud Run Job executes and writes FMIOutputEnvelope to GCS
6. Job POSTs callback to Django with result_uri
7. Callback downloads envelope and evaluates output-stage assertions

## Output Envelope Structure

The FMI Cloud Run Job produces an `FMIOutputEnvelope` (from
vb_shared.fmi.envelopes) containing:

- outputs.output_values: Dict keyed by catalog slug with simulation outputs
  - Each key is a catalog entry slug (e.g., "indoor_temp_c")
  - Values are the simulation outputs for that signal

These output values are extracted via `extract_output_signals()` for use in
output-stage CEL assertions (e.g., "indoor_temp_c < 26").

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


@register_engine(ValidationType.FMI)
class FMIValidationEngine(BaseValidatorEngine):
    """
    Run FMI validators through Cloud Run Jobs.

    This engine uploads the FMU and input bindings to GCS, triggers a Cloud Run
    Job that executes the FMU simulation, and receives results via callback.
    """

    @classmethod
    def extract_output_signals(cls, output_envelope: Any) -> dict[str, Any] | None:
        """
        Extract output values from an FMI output envelope.

        FMI envelopes (FMIOutputEnvelope from vb_shared) store simulation outputs
        in outputs.output_values as a dict keyed by catalog slug.

        Args:
            output_envelope: FMIOutputEnvelope from the Cloud Run Job.

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
            logger.debug("Could not extract assertion signals from FMI envelope")

        return None

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset | None,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """
        Validate an FMI submission.

        Launches a Cloud Run Job asynchronously and returns a pending result.

        Args:
            validator: FMI validator instance with FMU model attached
            submission: Submission with input values
            ruleset: Optional ruleset (not typically used for FMI)
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
                "FMI engine requires run_context to be set with "
                "validation_run and workflow_step"
            )
            issues = [
                ValidationIssue(
                    path="",
                    message=_(
                        "FMI validation requires workflow context. "
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

        # Cloud Run configuration required
        if not settings.GCS_VALIDATION_BUCKET or not settings.GCS_FMI_JOB_NAME:
            logger.warning(
                "Cloud Run Jobs not configured for FMI - "
                "returning not-implemented error"
            )
            issues = [
                ValidationIssue(
                    path="",
                    message=_(
                        "FMI Cloud Run Jobs not configured. "
                        "Set GCS_VALIDATION_BUCKET and GCS_FMI_JOB_NAME "
                        "in production settings.",
                    ),
                    severity=Severity.ERROR,
                ),
            ]
            return ValidationResult(
                passed=False,
                issues=issues,
                stats={"implementation_status": "FMI Cloud Run not configured"},
            )

        # Import here to avoid circular dependency
        from validibot.validations.services.cloud_run.launcher import (
            launch_fmi_validation,
        )

        return launch_fmi_validation(
            run=run,
            validator=validator,
            submission=submission,
            ruleset=ruleset,
            step=step,
        )
