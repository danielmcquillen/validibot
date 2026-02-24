"""
Base classes and protocols for assertion evaluators.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any
from typing import Protocol

if TYPE_CHECKING:
    from validibot.validations.models import RulesetAssertion
    from validibot.validations.models import Validator
    from validibot.validations.validators.base import BaseValidator
    from validibot.validations.validators.base import ValidationIssue


@dataclass
class AssertionContext:
    """
    Context available during assertion evaluation.

    Provides access to the validator configuration, the validator instance
    (for shared utilities like _maybe_success_issue), and a lazily-built
    CEL context for CEL assertions.

    Attributes:
        validator: The Validator model instance with catalog entries.
        engine: The validator instance for shared utilities.
        cel_context: CEL evaluation context, built lazily on first CEL assertion.
    """

    validator: Validator
    engine: BaseValidator
    cel_context: dict[str, Any] | None = field(default=None)

    def get_cel_context(self, payload: Any) -> dict[str, Any]:
        """
        Get or build the CEL context for the given payload.

        The context is built once and cached for reuse across multiple
        CEL assertions in the same evaluation pass.
        """
        if self.cel_context is None:
            self.cel_context = self.engine._build_cel_context(payload, self.validator)
        return self.cel_context


class AssertionEvaluator(Protocol):
    """
    Protocol for assertion type evaluators.

    Each assertion type (BASIC, CEL, future types) implements this protocol
    to provide type-specific evaluation logic.
    """

    def evaluate(
        self,
        *,
        assertion: RulesetAssertion,
        payload: Any,
        context: AssertionContext,
    ) -> list[ValidationIssue]:
        """
        Evaluate a single assertion and return any issues.

        Args:
            assertion: The RulesetAssertion model instance to evaluate.
            payload: The data to evaluate the assertion against.
            context: Evaluation context with validator and CEL context.

        Returns:
            List of ValidationIssue objects. Empty list means the assertion passed.
            May include a SUCCESS-severity issue if success messages are enabled.
        """
        ...
