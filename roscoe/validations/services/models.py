from dataclasses import dataclass, field

from roscoe.validations.constants import StepStatus, ValidationRunStatus


@dataclass
class ValidationStepSummary:
    step_id: int
    name: str
    status: StepStatus
    issues: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def issue_count(self) -> int:
        return len(self.issues)


@dataclass
class ValidationRunSummary:
    overview: str
    steps: list[ValidationStepSummary]


@dataclass
class ValidationRunTaskResult:
    run_id: int
    status: ValidationRunStatus
    summary: ValidationRunSummary | None = None
    error: str | None = None
