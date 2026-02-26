"""
Validation step processor module.

This module provides the ValidationStepProcessor abstraction for executing
validation steps. Processors handle lifecycle orchestration while validators
handle validation logic and assertion evaluation.

Key classes:
- ValidationStepProcessor: Base class with shared infrastructure
- SimpleValidationProcessor: For inline validators (JSON, XML, Basic, THERM)
- AdvancedValidationProcessor: For validators requiring dedicated compute
  (container-based: EnergyPlus, FMU; compute-intensive: AI)
- StepProcessingResult: Dataclass for processor return values
"""

from validibot.validations.services.step_processor.advanced import (
    AdvancedValidationProcessor,
)
from validibot.validations.services.step_processor.base import ValidationStepProcessor
from validibot.validations.services.step_processor.factory import get_step_processor
from validibot.validations.services.step_processor.result import StepProcessingResult
from validibot.validations.services.step_processor.simple import (
    SimpleValidationProcessor,
)

__all__ = [
    "AdvancedValidationProcessor",
    "SimpleValidationProcessor",
    "StepProcessingResult",
    "ValidationStepProcessor",
    "get_step_processor",
]
