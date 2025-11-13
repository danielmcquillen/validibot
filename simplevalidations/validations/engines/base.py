"""
Base classes and data structures for validator engines.

A validator engine is a class that subclasses BaseValidatorEngine and implements
the validate() method. The subclass is what does the actual validation work
in a given validation step.

You won't find any concrete implementations here; those are in other modules.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from dataclasses import asdict
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from simplevalidations.validations.cel import DEFAULT_HELPERS
from simplevalidations.validations.cel import CelHelper
from simplevalidations.validations.constants import Severity
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.providers import get_provider_for_validator

if TYPE_CHECKING:
    from simplevalidations.submissions.models import Submission
    from simplevalidations.validations.models import Ruleset
    from simplevalidations.validations.models import Validator


@dataclass
class ValidationIssue:
    """
    Represents a single validation problem emitted by an engine.

    Attributes:
        path: JSON Pointer / XPath / dotted path for the failing value.
        message: Human readable description of the problem.
        severity: INFO/WARNING/ERROR (default ERROR).
        code: Optional machine-readable string for grouping (e.g. "json.required").
        meta: Optional loose metadata used to enrich ValidationFinding rows.
        assertion_id: Optional RulesetAssertion PK when the issue was produced
            by a structured assertion.
    """

    path: str
    message: str
    severity: Severity = Severity.ERROR
    code: str = ""
    meta: dict[str, Any] | None = None
    assertion_id: int | None = None

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
    cel_helpers = DEFAULT_HELPERS

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        # Arbitrary configuration (e.g., schema, thresholds, flags)
        self.config: dict[str, Any] = config or {}
        self.processor_name: str = self.config.get("processor_name", "").strip()

    def get_cel_helpers(self) -> dict[str, "CelHelper"]:
        """
        Return the helper allowlist for this engine. Subclasses can override to
        append or remove helpers based on validator metadata.
        """
        return dict(self.cel_helpers)

    def resolve_provider(self, validator: "Validator"):
        """
        Resolve the provider configured for the given validator, if any.
        """
        return get_provider_for_validator(validator)

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
