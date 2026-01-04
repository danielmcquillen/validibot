"""
EnergyPlus validation engine powered by Cloud Run Jobs.

This engine forwards incoming EnergyPlus submissions (epJSON or IDF) to
Cloud Run Jobs and receives results via callbacks.

The validation flow:
1. Engine receives validator, submission, ruleset from workflow execution
2. run_context is set by the handler with validation_run and workflow_step
3. If Cloud Run Jobs is configured, launches async job via launcher
4. Returns pending ValidationResult
5. Cloud Run Job executes and POSTs callback to Django
6. Callback updates ValidationRun with final results

If Cloud Run Jobs is not configured (local dev), returns not-implemented error.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

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
        provider = self.resolve_provider(validator)
        if provider:
            provider.ensure_catalog_entries()

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
