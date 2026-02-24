"""
CEL expression assertion evaluator.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _

from validibot.validations.assertions.evaluators.registry import register_evaluator
from validibot.validations.cel_eval import evaluate_cel_expression
from validibot.validations.constants import CEL_MAX_CONTEXT_SYMBOLS
from validibot.validations.constants import CEL_MAX_EVAL_TIMEOUT_MS
from validibot.validations.constants import CEL_MAX_EXPRESSION_CHARS
from validibot.validations.constants import AssertionType
from validibot.validations.validators.base import ValidationIssue

if TYPE_CHECKING:
    from validibot.validations.assertions.evaluators.base import AssertionContext
    from validibot.validations.models import RulesetAssertion

logger = logging.getLogger(__name__)


@register_evaluator(AssertionType.CEL_EXPRESSION)
class CelAssertionEvaluator:
    """
    Evaluates CEL expression assertions.

    CEL (Common Expression Language) assertions use expressions stored in
    assertion.rhs["expr"] or assertion.cel_cache. The expression is evaluated
    against a context built from the validator's catalog entries and the payload.
    """

    def evaluate(
        self,
        *,
        assertion: RulesetAssertion,
        payload: Any,
        context: AssertionContext,
    ) -> list[ValidationIssue]:
        """
        Evaluate a single CEL assertion.

        Args:
            assertion: The CEL assertion to evaluate.
            payload: The data to evaluate against.
            context: Evaluation context with validator and CEL context.

        Returns:
            List of ValidationIssue objects (empty if passed without success message).
        """
        # Get or build the CEL evaluation context
        try:
            cel_context = context.get_cel_context(payload)
        except Exception as exc:
            return [
                self._issue_from_assertion(
                    assertion,
                    path="",
                    message=_("Unable to build CEL context: %(err)s") % {"err": exc},
                ),
            ]

        # Get the expression to evaluate
        expr = (assertion.rhs or {}).get("expr") or assertion.cel_cache or ""

        # Validate expression length
        if len(expr) > CEL_MAX_EXPRESSION_CHARS:
            return [
                self._issue_from_assertion(
                    assertion,
                    path="",
                    message=_("CEL expression is too long."),
                ),
            ]

        # Validate context size
        if len(cel_context) > CEL_MAX_CONTEXT_SYMBOLS:
            return [
                self._issue_from_assertion(
                    assertion,
                    path="",
                    message=_("CEL context is too large."),
                ),
            ]

        # Evaluate optional guard expression
        when_expr = (assertion.when_expression or "").strip()
        if when_expr:
            guard_result = evaluate_cel_expression(
                expression=when_expr,
                context=cel_context,
                timeout_ms=CEL_MAX_EVAL_TIMEOUT_MS,
            )
            if not guard_result.success:
                return [
                    self._issue_from_assertion(
                        assertion,
                        path="",
                        message=_("CEL 'when' failed: %(err)s")
                        % {"err": guard_result.error},
                    ),
                ]
            if not guard_result.value:
                # Guard condition not met - skip this assertion
                return []

        # Evaluate the main expression
        result = evaluate_cel_expression(
            expression=expr,
            context=cel_context,
            timeout_ms=CEL_MAX_EVAL_TIMEOUT_MS,
        )

        if not result.success:
            # Expression evaluation failed
            msg = self._format_error_message(str(result.error))
            return [
                self._issue_from_assertion(
                    assertion,
                    path="",
                    message=_("CEL evaluation failed: %(err)s") % {"err": msg},
                ),
            ]

        if not bool(result.value):
            # Expression evaluated to false - assertion failed
            failure_message = assertion.message_template or _(
                "CEL assertion evaluated to false.",
            )
            return [
                self._issue_from_assertion(
                    assertion,
                    path="",
                    message=failure_message,
                ),
            ]

        # Assertion passed - emit success issue if configured
        success_issue = context.engine._maybe_success_issue(assertion)
        if success_issue:
            return [success_issue]

        return []

    def _issue_from_assertion(
        self,
        assertion: RulesetAssertion,
        path: str,
        message: str,
    ) -> ValidationIssue:
        """Create a ValidationIssue from an assertion failure."""
        return ValidationIssue(
            path=path,
            message=message,
            severity=assertion.severity,
            code=assertion.operator,
            meta={"ruleset_id": assertion.ruleset_id},
            assertion_id=getattr(assertion, "id", None),
        )

    def _format_error_message(self, raw_error: str) -> str:
        """Format CEL error messages for better user readability."""
        # Try to extract undefined identifier from error message
        missing_ref = re.search(
            r"undeclared reference to ['\"](?P<ident>[^'\"]+)['\"]",
            raw_error,
        )
        identifier = None
        if missing_ref:
            identifier = missing_ref.group("ident")
        elif "undeclared reference to" in raw_error:
            tail = raw_error.split("undeclared reference to", 1)[1]
            identifier = tail.strip().split()[0].strip(" '\"()\\")

        if identifier:
            return _(
                "CEL references undefined identifier '%(identifier)s'. "
                "Ensure a matching validator catalog entry exists."
            ) % {"identifier": identifier}

        return raw_error
