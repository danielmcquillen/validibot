from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field

from simplevalidations.validations.constants import StepStatus
from simplevalidations.validations.constants import ValidationRunStatus


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

    def to_payload(self) -> dict[str, object]:
        """
        Return a JSON-serializable representation of the task result.
        """
        payload: dict[str, object] = {
            "run_id": str(self.run_id),
            "status": str(self.status),
            "error": self.error,
        }
        if self.summary:
            payload["summary"] = asdict(self.summary)
        return payload
