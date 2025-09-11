from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from roscoe.validations.constants import Severity, ValidationType

if TYPE_CHECKING:
    from roscoe.submissions.models import Submission
    from roscoe.validations.models import Ruleset, Validator


@dataclass
class ValidationIssue:
    """
    Represents a single validation problem.
    path: JSONPointer-like path (e.g., zones/0/area_m2) or XPath for XML, etc.
    message: human-readable description of the problem.
    severity: INFO/WARNING/ERROR (default ERROR).
    """

    path: str
    message: str
    severity: Severity = Severity.ERROR

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationResult:
    """
    Aggregated result of a single validation step.
    passed: True when no ERROR issues were produced.
    issues: list of issues discovered (may include INFO/WARNING).
    stats: optional extra info (counts, timings, metadata).
    """

    passed: bool
    issues: list[ValidationIssue]
    workflow_step_name: str | None = None  # slug
    stats: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "issues": [i.to_dict() for i in self.issues],
            "stats": self.stats or {},
        }


class BaseValidatorEngine(ABC):
    """
    Base class for all validator enginge implementations....the code that
    actually does the validation logic.
    Concrete subclasses should be registered in the registry keyed by ValidationType.

    To keep validator engine classes clean, we pass everything it
    needs either via the config dict or the ContentSource.
    We don't pass in any model instances.
    """

    validation_type: ValidationType

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        # Arbitrary configuration (e.g., schema, thresholds, flags)
        self.config: dict[str, Any] = config or {}

    @abstractmethod
    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset,
    ) -> ValidationResult:
        """
        Run standard, defined validator on a submission by an API user,
        using a ruleset defined by the author.
        """
        raise NotImplementedError
