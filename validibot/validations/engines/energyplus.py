"""
EnergyPlus validation engine powered by Cloud Run Jobs.

This engine forwards incoming EnergyPlus submissions (epJSON or IDF) to
Cloud Run Jobs and receives results via callbacks.

The validation flow:
1. Engine receives validator, submission, ruleset from workflow execution
2. If Cloud Run Jobs is configured, launches async job via launcher
3. Returns pending ValidationResult
4. Cloud Run Job executes and POSTs callback to Django
5. Callback updates ValidationRun with final results

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
    from validibot.submissions.models import Submission
    from validibot.validations.models import Ruleset
    from validibot.validations.models import ValidationRun
    from validibot.validations.models import Validator
    from validibot.workflows.models import WorkflowStep


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
    ) -> ValidationResult:
        """
        Validate an EnergyPlus submission.

        This is the original signature that doesn't have access to ValidationRun.
        Returns not-implemented error and directs to use validate_with_run instead.
        """
        provider = self.resolve_provider(validator)
        if provider:
            provider.ensure_catalog_entries()

        issues = [
            ValidationIssue(
                path="",
                message=_(
                    "EnergyPlus validation requires Cloud Run Jobs integration. "
                    "Use validate_with_run() instead of validate().",
                ),
                severity=Severity.ERROR,
            ),
        ]

        stats = {
            "implementation_status": "Requires validate_with_run()",
            "executor": "Cloud Run Jobs",
        }

        return ValidationResult(passed=False, issues=issues, stats=stats)

    def validate_with_run(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset | None,
        run: ValidationRun,
        step: WorkflowStep,
    ) -> ValidationResult:
        """
        Validate an EnergyPlus submission with ValidationRun context.

        This is called by the validation workflow when it has created a ValidationRun.
        It launches a Cloud Run Job asynchronously and returns a pending result.

        Args:
            validator: EnergyPlus validator instance
            submission: Submission with IDF/epJSON content
            ruleset: Ruleset with weather_file metadata
            run: ValidationRun instance (in PENDING status)
            step: WorkflowStep with configuration

        Returns:
            ValidationResult with passed=None (pending) if Cloud Run Jobs configured,
            or passed=False (error) if not configured for local development.
        """
        provider = self.resolve_provider(validator)
        if provider:
            provider.ensure_catalog_entries()

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
