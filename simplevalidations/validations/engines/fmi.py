"""
FMI validation engine powered by Cloud Run Jobs.

This engine will forward incoming FMU submissions to Cloud Run Jobs and
translate the response into Validibot issues.

TODO: Phase 4 - Implement full Cloud Run Jobs integration for FMI validators.
For now, this is a placeholder that will be implemented in Phase 4.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils.translation import gettext as _

from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.engines.base import BaseValidatorEngine
from simplevalidations.validations.engines.base import ValidationResult
from simplevalidations.validations.engines.registry import register_engine

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from simplevalidations.submissions.models import Submission
    from simplevalidations.validations.models import Ruleset
    from simplevalidations.validations.models import Validator


@register_engine(ValidationType.FMI)
class FMIValidationEngine(BaseValidatorEngine):
    """
    Run FMI validators through Cloud Run Jobs and translate the
    response into Validibot issues.

    TODO: Phase 4 - Full Cloud Run Jobs implementation.
    For now, this is a placeholder that returns a not-implemented error.
    """

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset | None,
    ) -> ValidationResult:
        """
        Validate an FMI submission.

        """
        provider = self.resolve_provider(validator)
        if provider:
            provider.ensure_catalog_entries()

        from django.conf import settings

        # Cloud Run configuration required
        if not settings.GCS_VALIDATION_BUCKET or not settings.GCS_FMI_JOB_NAME:
            logger.warning(
                "Cloud Run Jobs not configured for FMI - returning not-implemented error"
            )
            return ValidationResult(
                passed=False,
                issues=[],
                stats={
                    "implementation_status": "FMI Cloud Run not configured",
                },
            )

        # Import here to avoid circular dependency
        from simplevalidations.validations.services.cloud_run.launcher import (
            launch_fmi_validation,
        )

        return launch_fmi_validation(
            run=self.run_context.validation_run,
            validator=validator,
            submission=submission,
            ruleset=ruleset,
            step=self.run_context.workflow_step,
        )
