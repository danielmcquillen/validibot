"""
EnergyPlus validation engine powered by Cloud Run Jobs.

This engine will forward incoming EnergyPlus submissions (epJSON or IDF) to
Cloud Run Jobs and translate the response into Validibot issues.

TODO: Phase 4 - Implement full Cloud Run Jobs integration:
- Create ValidationRun instances
- Upload input envelopes to GCS
- Trigger Cloud Run Jobs via Cloud Tasks
- Receive callbacks from validators
- Download output envelopes and translate to ValidationResult

For now, this is a placeholder that will be implemented in Phase 4.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils.translation import gettext as _

from simplevalidations.validations.constants import Severity
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.engines.base import BaseValidatorEngine
from simplevalidations.validations.engines.base import ValidationIssue
from simplevalidations.validations.engines.base import ValidationResult
from simplevalidations.validations.engines.registry import register_engine

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from simplevalidations.submissions.models import Submission
    from simplevalidations.validations.models import Ruleset
    from simplevalidations.validations.models import Validator


@register_engine(ValidationType.ENERGYPLUS)
class EnergyPlusValidationEngine(BaseValidatorEngine):
    """
    Run submitted epJSON through Cloud Run Jobs and translate the
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
        Validate an EnergyPlus submission.

        TODO: Phase 4 implementation:
        1. Create ValidationRun instance
        2. Build EnergyPlusInputEnvelope using envelope_builder
        3. Upload envelope to GCS
        4. Trigger Cloud Run Job via Cloud Tasks
        5. Return ValidationResult with pending status
        6. Callback will update ValidationRun when complete

        For now, returns not-implemented error.
        """
        provider = self.resolve_provider(validator)
        if provider:
            provider.ensure_catalog_entries()

        issues = [
            ValidationIssue(
                path="",
                message=_(
                    "EnergyPlus Cloud Run Jobs integration is not yet implemented. "
                    "This will be completed in Phase 4.",
                ),
                severity=Severity.ERROR,
            ),
        ]

        stats = {
            "implementation_status": "Phase 4 - Not yet implemented",
            "planned_executor": "Cloud Run Jobs",
        }

        return ValidationResult(passed=False, issues=issues, stats=stats)
