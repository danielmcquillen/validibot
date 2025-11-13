from dataclasses import dataclass

from simplevalidations.validations.constants import ValidationRunStatus


@dataclass
class ValidationRunTaskResult:
    run_id: int
    status: ValidationRunStatus
    error: str | None = None

    def to_payload(self) -> dict[str, object]:
        """
        Return a JSON-serializable representation of the task result.
        """
        return {
            "run_id": str(self.run_id),
            "status": str(self.status),
            "error": self.error,
        }
