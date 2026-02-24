"""
Factory function for getting the appropriate step processor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from validibot.validations.constants import ValidationType
from validibot.validations.services.step_processor.advanced import (
    AdvancedValidationProcessor,
)
from validibot.validations.services.step_processor.simple import (
    SimpleValidationProcessor,
)

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
    based on the validator type.

    Args:
        validation_run: The ValidationRun model instance
        step_run: The ValidationStepRun model instance

    Returns:
        The appropriate processor instance for this step
    """
    validator = step_run.workflow_step.validator

    # Advanced validators run in containers
    advanced_types = {
        ValidationType.ENERGYPLUS,
        ValidationType.FMU,
    }

    if validator.validation_type in advanced_types:
        return AdvancedValidationProcessor(validation_run, step_run)
    return SimpleValidationProcessor(validation_run, step_run)
