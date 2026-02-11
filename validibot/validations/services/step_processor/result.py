"""
Step processing result dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from collections import Counter

    from validibot.validations.models import ValidationStepRun


@dataclass
class StepProcessingResult:
    """
    Result of processing a validation step.

    Returned by ValidationStepProcessor.execute() and complete_from_callback().

    Attributes:
        passed: True/False for complete, None for async (waiting for callback)
        step_run: The ValidationStepRun model instance
        severity_counts: Counter of findings by severity level
        total_findings: Total number of ValidationFinding records created
        assertion_failures: Number of assertions that failed
        assertion_total: Total number of assertions evaluated
    """

    passed: bool | None
    step_run: ValidationStepRun
    severity_counts: Counter[Any]
    total_findings: int
    assertion_failures: int
    assertion_total: int
