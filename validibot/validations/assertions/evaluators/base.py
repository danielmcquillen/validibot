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
    from datetime import datetime

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
        stage: The assertion evaluation stage ("input" or "output").
        cel_context: CEL evaluation context, built lazily on first CEL assertion.
        now: The run's pinned evaluation clock (``run.started_at``) used to bind
            CEL ``now()``. ``None`` when there is no run context (e.g. a direct
            unit-test call), in which case an expression using ``now()`` fails
            cleanly rather than reading the wall clock — matching the tabular
            row-stage behavior so ``now()`` is deterministic for the whole run.
    """

    validator: Validator
    engine: BaseValidator
    stage: str = "input"
    cel_context: dict[str, Any] | None = field(default=None)
    enriched_payload: Any = field(default=None)
    now: datetime | None = field(default=None)

    def get_cel_context(self, payload: Any) -> dict[str, Any]:
        """
        Get or build the CEL context for the given payload.

        The context is built once and cached for reuse across multiple
        CEL assertions in the same evaluation pass.
        """
        if self.cel_context is None:
            self.cel_context = self.engine._build_cel_context(
                payload, self.validator, stage=self.stage
            )
        return self.cel_context

    def get_enriched_payload(self, payload: Any) -> Any:
        """Get or build the namespace-enriched payload for BASIC assertions.

        BASIC assertions walk a dotted path against the payload, so the
        namespaced values must be merged in first: ``s.*``/``i.*``/``o.*``
        flattened to bare keys, plus a nested ``submission`` sub-dict. Without
        this, a BASIC target like ``submission.metadata.deliverable`` or
        ``s.target_eui`` resolves to "not found" for any validator that hands
        the evaluator a raw payload (JSON Schema, XML Schema, the Tabular
        generic lane) — even though the same reference works in CEL.

        Centralizing the enrichment here (rather than at each validator's call
        site) guarantees the property for every validator and every future one.
        It is **idempotent**: validators that already enrich before dispatch
        (Basic, THERM, and the advanced validators) pass an already-enriched
        dict, and re-running the merge is a no-op (the values are present, so
        the ``setdefault`` merges do nothing and ``submission`` is re-injected
        with identical data). Built once per stage and cached, mirroring
        ``get_cel_context``; ``_build_cel_context`` is unaffected, so CEL keeps
        seeing the raw payload under ``p`` with ``submission`` as its own key.
        """
        if self.enriched_payload is None:
            self.enriched_payload = self.engine._enrich_basic_payload(
                payload, stage=self.stage
            )
        return self.enriched_payload


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
