"""
FMI validation engine powered by Cloud Run Jobs.

This engine forwards FMU submissions to Cloud Run Jobs for execution and
translates the response into Validibot issues. The FMU is executed in a
containerized environment with the FMI runtime.

The validation flow:
1. Engine receives validator, submission, ruleset from workflow execution
2. run_context is set by the handler with validation_run and workflow_step
3. If Cloud Run Jobs is configured, launches async job via launcher
4. Returns pending ValidationResult
5. Cloud Run Job executes and POSTs callback to Django
6. Callback updates ValidationRun with final results
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


@register_engine(ValidationType.FMI)
class FMIValidationEngine(BaseValidatorEngine):
    """
    Run FMI validators through Cloud Run Jobs.

    This engine uploads the FMU and input bindings to GCS, triggers a Cloud Run
    Job that executes the FMU simulation, and receives results via callback.
    """

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
