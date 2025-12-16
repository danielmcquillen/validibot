from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol

from dataclasses import dataclass
from dataclasses import field

if TYPE_CHECKING:
    from validibot.validations.models import ValidationRun
    from validibot.workflows.models import WorkflowStep


@dataclass
class RunContext:
    """Context passed to every step handler during execution."""
    
    validation_run: ValidationRun
    step: WorkflowStep
    downstream_signals: dict[str, Any] = field(default_factory=dict)
    # Add other shared context here (e.g. user, dry_run flag, etc.)


@dataclass
class StepResult:
    """Standardized result from any step execution (Validator or Action)."""
    
    passed: bool | None  # None = Async Pending
    issues: list[Any] = field(default_factory=list)  # ValidationIssue list
    stats: dict[str, Any] = field(default_factory=dict)
    
    # Optional: Handler-specific outputs that aren't stats
    outputs: dict[str, Any] = field(default_factory=dict)


class StepHandler(Protocol):
    """Protocol for any class that handles the execution of a workflow step."""

    def execute(
        self,
        context: RunContext,
    ) -> StepResult:
        """
        Execute the business logic for this step.
        
        Args:
            context: The run context containing the step, run, and signals.
            
        Returns:
            StepResult indicating success/failure and any outputs.
        """
        ...
