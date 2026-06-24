"""
Factory function for getting the appropriate step processor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from validibot.validations.constants import ADVANCED_VALIDATION_TYPES
from validibot.validations.services.step_processor.advanced import (
    AdvancedValidationProcessor,
)
from validibot.validations.services.step_processor.simple import (
    SimpleValidationProcessor,
)
from validibot.validations.validators.base.config import get_config

if TYPE_CHECKING:
    from validibot.validations.models import ValidationRun
    from validibot.validations.models import ValidationStepRun
    from validibot.validations.services.step_processor.base import (
        ValidationStepProcessor,
    )


def get_step_processor(
    validation_run: ValidationRun,
    step_run: ValidationStepRun,
) -> ValidationStepProcessor:
    """
    Get the appropriate processor for a validation step.

    Routes to SimpleValidationProcessor or AdvancedValidationProcessor
    based on registered validator capabilities.

    Args:
        validation_run: The ValidationRun model instance
        step_run: The ValidationStepRun model instance

    Returns:
        The appropriate processor instance for this step
    """
    validator = step_run.workflow_step.validator
    config = get_config(validator.validation_type)

    if config is not None and (
        config.output_envelope_class or config.resolved_envelope_class
    ):
        return AdvancedValidationProcessor(validation_run, step_run)
    if validator.validation_type in ADVANCED_VALIDATION_TYPES:
        return AdvancedValidationProcessor(validation_run, step_run)
    return SimpleValidationProcessor(validation_run, step_run)
