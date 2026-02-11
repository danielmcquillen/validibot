from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol

if TYPE_CHECKING:
    from validibot.validations.models import ValidationRun
    from validibot.workflows.models import WorkflowStep


@dataclass
class RunContext:
    """
    Context passed to step handlers and validator engines during execution.

    This dataclass provides execution context for both workflow step handlers
    (via StepHandler.execute()) and validator engines (via
    BaseValidatorEngine.validate()).

    For step handlers, all fields are required. For validator engines, the context
    is optional - sync engines (XML, JSON, Basic, AI) typically don't need it,
    while async engines (EnergyPlus, FMI) require it for job tracking.

    Attributes:
        validation_run: The ValidationRun model instance being executed.
        step: The WorkflowStep model instance being processed.
        downstream_signals: Signals from previous workflow steps, keyed by step slug.
            Used for CEL cross-step assertions.
    """

    validation_run: ValidationRun | None = None
    step: WorkflowStep | None = None
    downstream_signals: dict[str, Any] = field(default_factory=dict)


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
        run_context: RunContext,
    ) -> StepResult:
        """
        Execute the business logic for this step.

        Args:
            run_context: The run context containing the step, run, and signals.

        Returns:
            StepResult indicating success/failure and any outputs.
        """
        ...
