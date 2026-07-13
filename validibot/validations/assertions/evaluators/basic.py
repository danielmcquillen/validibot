"""
BASIC assertion evaluator.

This evaluator handles BASIC assertions with operator dispatch to specialized
methods for each operator type (equality, comparison, membership, string ops, etc.).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from typing import Any

from django.utils.html import strip_tags
from django.utils.translation import gettext as _

from validibot.validations.assertions.evaluators.registry import register_evaluator
from validibot.validations.assertions.message_templates import (
    MessageTemplateRenderError,
)
from validibot.validations.assertions.message_templates import (
    render_assertion_message_template,
)
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.regex_safety import UnsafeOrInvalidPatternError
from validibot.validations.regex_safety import compile_user_pattern
from validibot.validations.validators.base import ValidationIssue

if TYPE_CHECKING:
    from validibot.validations.assertions.evaluators.base import AssertionContext
    from validibot.validations.models import RulesetAssertion


@register_evaluator(AssertionType.BASIC)
class BasicAssertionEvaluator:
    """
    Evaluates BASIC assertions with operator dispatch.

    BASIC assertions target a specific field in the payload and apply an operator
    (EQ, NE, LT, CONTAINS, etc.) to compare the actual value against expected values.
    """

    def evaluate(
        self,
        *,
        assertion: RulesetAssertion,
        payload: Any,
        context: AssertionContext,
    ) -> list[ValidationIssue]:
        """
        Evaluate a single BASIC assertion.

        Args:
            assertion: The BASIC assertion to evaluate.
            payload: The data to evaluate against.
            context: Evaluation context with validator.

        Returns:
            List of ValidationIssue objects (empty if passed without success message).
        """
        path = self._assertion_path(assertion)
        # Resolve against the namespace-enriched payload so BASIC targets like
        # ``submission.metadata.deliverable`` and ``s.target_eui`` work for
        # EVERY validator — including JSON Schema, XML Schema and the Tabular
        # generic lane, which hand the evaluator a raw/empty payload. The
        # enrichment is cached per stage and is idempotent for validators that
        # already enriched before dispatch. See AssertionContext.get_enriched_payload.
        enriched_payload = context.get_enriched_payload(payload)
        actual, found = self._resolve_path(enriched_payload, path)
        options = assertion.options or {}

        if not found and not options.get("treat_missing_as_null"):
            return [
                self._issue_from_assertion(
                    assertion,
                    path or "",
                    _("Value for '%(path)s' was not found.") % {"path": path},
                ),
            ]

        passed, failure_message = self._evaluate_basic_assertion(
            operator=assertion.operator,
            actual=actual,
            rhs=assertion.rhs or {},
            options=options,
        )

        template_context = self._build_template_context(
            assertion=assertion,
            path=path,
            actual=actual,
            rhs=assertion.rhs or {},
            options=options,
            enriched_payload=enriched_payload,
        )

        if not passed:
            message = self._render_message(
                assertion,
                template_context,
                failure_message,
                actual,
            )
            return [
                self._issue_from_assertion(
                    assertion,
                    path or "",
                    message,
                ),
            ]

        # Assertion passed - emit success issue if configured
        success_issue = context.engine._maybe_success_issue(
            assertion,
            template_context=template_context,
        )
        if success_issue:
            return [success_issue]

        return []

    # ------------------------------------------------------------------ Helpers

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

    def _assertion_path(self, assertion: RulesetAssertion) -> str:
        """Get the target path for an assertion.

        Checks target_signal_definition first, then falls back to
        target_data_path (custom free-form targets).
        """
        if assertion.target_signal_definition_id:
            return assertion.target_signal_definition.contract_key
        return assertion.target_data_path

    def _resolve_path(self, data: Any, path: str | None) -> tuple[Any, bool]:
        """Resolve a dot/bracket path in the data.

        Delegates to the shared ``resolve_path()`` function in
        ``validations.services.path_resolution``. This method is kept
        as a thin wrapper so the evaluator API remains unchanged.

        Args:
            data: The payload to navigate.
            path: Path like ``"foo.bar[0].baz"``.

        Returns:
            Tuple of ``(resolved_value, was_found)``.
        """
        from validibot.validations.services.path_resolution import resolve_path

        return resolve_path(data, path)

    # ------------------------------------------------------ Operator dispatch

    def _evaluate_basic_assertion(
        self,
        *,
        operator: str,
        actual: Any,
        rhs: dict[str, Any],
        options: dict[str, Any],
    ) -> tuple[bool, str]:
        """
        Dispatch to the appropriate operator evaluator.

        Returns:
            Tuple of (passed, failure_message).
        """
        op = AssertionOperator(operator)

        if op in {AssertionOperator.EQ, AssertionOperator.NE}:
            return self._evaluate_equality(op, actual, rhs, options)

        if op in {
            AssertionOperator.LT,
            AssertionOperator.LE,
            AssertionOperator.GT,
            AssertionOperator.GE,
        }:
            return self._evaluate_comparison(op, actual, rhs, options)

        if op in {AssertionOperator.BETWEEN, AssertionOperator.COUNT_BETWEEN}:
            return self._evaluate_between(op, actual, rhs, options)

        if op in {AssertionOperator.IN, AssertionOperator.NOT_IN}:
            return self._evaluate_membership(op, actual, rhs, options)

        if op in {
            AssertionOperator.CONTAINS,
            AssertionOperator.NOT_CONTAINS,
            AssertionOperator.STARTS_WITH,
            AssertionOperator.ENDS_WITH,
        }:
            return self._evaluate_string_operator(op, actual, rhs, options)

        if op == AssertionOperator.MATCHES:
            return self._evaluate_regex(actual, rhs, options)

        if op in {AssertionOperator.IS_NULL, AssertionOperator.NOT_NULL}:
            is_null = actual is None
            passed = is_null if op == AssertionOperator.IS_NULL else not is_null
            return passed, _("Value was %(state)s.") % {
                "state": _("null") if is_null else _("not null"),
            }

        if op in {AssertionOperator.IS_EMPTY, AssertionOperator.NOT_EMPTY}:
            is_empty = not actual
            passed = is_empty if op == AssertionOperator.IS_EMPTY else not is_empty
            return passed, _("Value was %(state)s.") % {
                "state": _("empty") if is_empty else _("not empty"),
            }

        if op in {
            AssertionOperator.LEN_EQ,
            AssertionOperator.LEN_LE,
            AssertionOperator.LEN_GE,
        }:
            return self._evaluate_length(op, actual, rhs, options)

        if op == AssertionOperator.TYPE_IS:
            return self._evaluate_type(actual, rhs)

        if op == AssertionOperator.APPROX_EQ:
            return self._evaluate_approx(actual, rhs, options)

        if op in {AssertionOperator.ANY, AssertionOperator.ALL, AssertionOperator.NONE}:
            return self._evaluate_collection_quantifier(op, actual, rhs, options)

        if op == AssertionOperator.UNIQUE:
            return self._evaluate_unique(actual)

        if op in {AssertionOperator.SUBSET, AssertionOperator.SUPERSET}:
            return self._evaluate_set_relation(op, actual, rhs)

        return False, _("Operator '%(operator)s' is not supported yet.") % {
            "operator": operator,
        }

    # ---------------------------------------------------- Operator evaluators

    def _evaluate_equality(
        self,
        op: AssertionOperator,
        actual: Any,
        rhs: dict[str, Any],
        options: dict[str, Any],
    ) -> tuple[bool, str]:
        expected = rhs.get("value")
        actual_value, expected_value = self._normalize_operands(
            actual,
            expected,
            options,
        )
        passed = actual_value == expected_value
        if op == AssertionOperator.NE:
            passed = actual_value != expected_value
        return passed, self._comparison_message(actual_value, expected_value)

    def _evaluate_comparison(
        self,
        op: AssertionOperator,
        actual: Any,
        rhs: dict[str, Any],
        options: dict[str, Any],
    ) -> tuple[bool, str]:
        expected = rhs.get("value")
        coerce_types = bool(options.get("coerce_types"))
        actual_num = self._to_number(actual, allow_coerce=coerce_types)
        expected_num = self._to_number(expected, allow_coerce=True)
        if actual_num is None or expected_num is None:
            return False, _("Value is not numeric.")
        passed = {
            AssertionOperator.LT: actual_num < expected_num,
            AssertionOperator.LE: actual_num <= expected_num,
            AssertionOperator.GT: actual_num > expected_num,
            AssertionOperator.GE: actual_num >= expected_num,
        }[AssertionOperator(op)]
        return passed, self._comparison_message(actual_num, expected_num)

    def _evaluate_between(
        self,
        op: AssertionOperator,
        actual: Any,
        rhs: dict[str, Any],
        options: dict[str, Any],
    ) -> tuple[bool, str]:
        actual_num = actual_len = None
        if op == AssertionOperator.COUNT_BETWEEN:
            if not isinstance(actual, (list, tuple, set, dict, str)):
                return False, _("Value does not support counting.")
            actual_len = len(actual)
            actual_num = actual_len
        else:
            coerce_types = bool(options.get("coerce_types"))
            actual_num = self._to_number(actual, allow_coerce=coerce_types)
        low = self._to_number(rhs.get("min"), allow_coerce=True)
        high = self._to_number(rhs.get("max"), allow_coerce=True)
        if actual_num is None or low is None or high is None:
            return False, _("Value is not numeric.")
        include_min = options.get("include_min", True)
        include_max = options.get("include_max", True)
        lower_ok = actual_num >= low if include_min else actual_num > low
        upper_ok = actual_num <= high if include_max else actual_num < high
        passed = lower_ok and upper_ok
        return passed, _("Expected between %(low)s and %(high)s.") % {
            "low": low,
            "high": high,
        }

    def _evaluate_membership(
        self,
        op: AssertionOperator,
        actual: Any,
        rhs: dict[str, Any],
        options: dict[str, Any],
    ) -> tuple[bool, str]:
        values = rhs.get("values") or []
        actual_norm, expected_collection = self._normalize_operands(
            actual,
            values,
            options,
        )
        passed = actual_norm in expected_collection
        if op == AssertionOperator.NOT_IN:
            passed = actual_norm not in expected_collection
        return passed, _("Membership check failed.")

    def _evaluate_string_operator(
        self,
        op: AssertionOperator,
        actual: Any,
        rhs: dict[str, Any],
        options: dict[str, Any],
    ) -> tuple[bool, str]:
        expected = rhs.get("value", "")
        actual_text = self._normalize_string(actual, options)
        expected_text = self._normalize_string(expected, options)
        if actual_text is None or expected_text is None:
            return False, _("Value is not textual.")
        if op == AssertionOperator.CONTAINS:
            passed = expected_text in actual_text
        elif op == AssertionOperator.NOT_CONTAINS:
            passed = expected_text not in actual_text
        elif op == AssertionOperator.STARTS_WITH:
            passed = actual_text.startswith(expected_text)
        else:
            passed = actual_text.endswith(expected_text)
        return passed, _("String comparison failed.")

    def _evaluate_regex(
        self,
        actual: Any,
        rhs: dict[str, Any],
        options: dict[str, Any],
    ) -> tuple[bool, str]:
        pattern = rhs.get("pattern")
        if not pattern:
            return False, _("Regular expression is missing.")
        actual_text = self._normalize_string(actual, options)
        if actual_text is None:
            return False, _("Value is not textual.")
        # Match with RE2 (linear-time, no backtracking) so an author pattern run
        # against submitter data cannot ReDoS the worker. A thread-based timeout
        # cannot reliably stop a backtracking ``re`` match — CPython holds the GIL
        # through the C call — so RE2 replaces that approach rather than guarding it.
        try:
            compiled = compile_user_pattern(
                pattern,
                ignore_case=bool(options.get("case_insensitive")),
            )
        except UnsafeOrInvalidPatternError as exc:
            return False, _("Invalid regex: %(error)s") % {"error": exc}
        return compiled.search(actual_text) is not None, _("Regex comparison failed.")

    def _evaluate_length(
        self,
        op: AssertionOperator,
        actual: Any,
        rhs: dict[str, Any],
        options: dict[str, Any],
    ) -> tuple[bool, str]:
        try:
            length = len(actual)
        except TypeError:
            return False, _("Value has no length.")
        expected = self._to_number(rhs.get("value"), allow_coerce=True)
        if expected is None:
            return False, _("Length comparison target missing.")
        if op == AssertionOperator.LEN_EQ:
            passed = length == expected
        elif op == AssertionOperator.LEN_LE:
            passed = length <= expected
        else:
            passed = length >= expected
        return passed, _("Length comparison failed.")

    def _evaluate_type(
        self,
        actual: Any,
        rhs: dict[str, Any],
    ) -> tuple[bool, str]:
        expected = (rhs or {}).get("value")
        type_map = {
            "string": str,
            "number": (int, float),
            "boolean": bool,
            "array": (list, tuple),
            "object": dict,
        }
        expected_type = type_map.get(str(expected).lower())
        if not expected_type:
            return False, _(
                "Unsupported expected type '%(value)s'.",
            ) % {"value": expected}
        return isinstance(actual, expected_type), _("Type mismatch.")

    def _evaluate_approx(
        self,
        actual: Any,
        rhs: dict[str, Any],
        options: dict[str, Any],
    ) -> tuple[bool, str]:
        target = self._to_number(rhs.get("value"), allow_coerce=True)
        tolerance = self._to_number(rhs.get("tolerance"), allow_coerce=True)
        coerce_types = bool(options.get("coerce_types"))
        actual_num = self._to_number(actual, allow_coerce=coerce_types)
        if None in {target, tolerance, actual_num}:
            return False, _("Value is not numeric.")
        mode = options.get("tolerance_mode", "absolute")
        if mode == "percent":
            tolerance = abs(target) * (tolerance / 100)
        diff = abs(actual_num - target)
        return diff <= tolerance, _(
            "Difference %(diff)s exceeds tolerance %(tol)s.",
        ) % {
            "diff": diff,
            "tol": tolerance,
        }

    def _evaluate_collection_quantifier(
        self,
        op: AssertionOperator,
        actual: Any,
        rhs: dict[str, Any],
        options: dict[str, Any],
    ) -> tuple[bool, str]:
        if not isinstance(actual, (list, tuple)):
            return False, _("Value is not a collection.")
        nested_op = rhs.get("operator")
        nested_value = rhs.get("value")
        if not nested_op:
            return False, _("Nested operator missing.")
        for item in actual:
            passed, msg = self._evaluate_basic_assertion(
                operator=nested_op,
                actual=item,
                rhs={"value": nested_value},
                options=options,
            )
            if op == AssertionOperator.ANY and passed:
                return True, ""
            if op == AssertionOperator.ALL and not passed:
                return False, _("Not every element satisfied the condition.")
            if op == AssertionOperator.NONE and passed:
                return False, _("At least one element matched when none should.")
        if op == AssertionOperator.ANY:
            return False, _("No collection elements satisfied the condition.")
        return True, ""

    def _evaluate_unique(self, actual: Any) -> tuple[bool, str]:
        if not isinstance(actual, (list, tuple)):
            return False, _("Value is not a collection.")
        seen = []
        for item in actual:
            if item in seen:
                return False, _("Duplicate values detected.")
            seen.append(item)
        return True, ""

    def _evaluate_set_relation(
        self,
        op: AssertionOperator,
        actual: Any,
        rhs: dict[str, Any],
    ) -> tuple[bool, str]:
        if not isinstance(actual, (list, set, tuple)):
            return False, _("Value is not a collection.")
        actual_set = set(actual)
        expected_set = set(rhs.get("values") or [])
        if op == AssertionOperator.SUBSET:
            return actual_set.issubset(expected_set), _("Collection is not a subset.")
        return actual_set.issuperset(expected_set), _("Collection is not a superset.")

    # ------------------------------------------------ Normalization helpers

    def _normalize_string(self, value: Any, options: dict[str, Any]) -> Any:
        """Normalize a string value based on options."""
        if isinstance(value, str):
            text = value
            if options.get("unicode_fold") or options.get("case_insensitive"):
                text = text.casefold()
            return text
        return value

    def _normalize_operands(
        self,
        actual: Any,
        expected: Any,
        options: dict[str, Any],
    ) -> tuple[Any, Any]:
        """Normalize operands for comparison based on options."""
        coerce = options.get("coerce_types")
        actual_value = actual
        expected_value = expected
        if coerce:
            if isinstance(expected, (int, float)) or self._looks_numeric(expected):
                actual_value = self._to_number(actual, allow_coerce=True)
                expected_value = self._to_number(expected, allow_coerce=True)
        actual_value = self._normalize_string(actual_value, options)
        if isinstance(expected_value, list):
            expected_value = [
                self._normalize_string(val, options) for val in expected_value
            ]
        else:
            expected_value = self._normalize_string(expected_value, options)
        return actual_value, expected_value

    def _looks_numeric(self, value: Any) -> bool:
        """Check if a value looks numeric (is or can be converted to a number)."""
        if isinstance(value, (int, float)):
            return True
        result = False
        if isinstance(value, str):
            try:
                float(value)
                result = True
            except (TypeError, ValueError):
                pass
        return result

    def _to_number(self, value: Any, *, allow_coerce: bool = False) -> float | None:
        """Convert a value to a number, optionally coercing strings."""
        if isinstance(value, (int, float)):
            return float(value)
        if allow_coerce and isinstance(value, str):
            try:
                return float(value.strip())
            except (TypeError, ValueError):
                return None
        return None

    def _comparison_message(self, actual: Any, expected: Any) -> str:
        """Generate a comparison failure message."""
        return _("Actual value %(actual)s failed comparison against %(expected)s.") % {
            "actual": actual,
            "expected": expected,
        }

    def _default_message(self, assertion: RulesetAssertion, actual: Any) -> str:
        """Generate a default failure message."""
        return _("%(target)s expected %(condition)s but was %(actual)s.") % {
            "target": assertion.target_display or _("value"),
            "condition": assertion.condition_display,
            "actual": actual,
        }

    # ---------------------------------------------------- Message templating

    def _render_message(
        self,
        assertion: RulesetAssertion,
        context: dict[str, Any],
        fallback_message: str | None,
        actual: Any,
    ) -> str:
        """Render the assertion message using template or fallback."""
        template = (assertion.message_template or "").strip()
        message: str | None = None
        if template:
            try:
                rendered = render_assertion_message_template(template, context)
            except MessageTemplateRenderError:
                message = _("Message template error - falling back to default output.")
            else:
                if rendered:
                    message = rendered
        if message is None:
            message = fallback_message or self._default_message(assertion, actual)
        return strip_tags(str(message))

    def _build_template_context(
        self,
        *,
        assertion: RulesetAssertion,
        path: str | None,
        actual: Any,
        rhs: dict[str, Any],
        options: dict[str, Any],
        enriched_payload: Any,
    ) -> dict[str, Any]:
        """Build a context dict for message template rendering."""
        constants = {}
        payload_context = {}
        submission_context = {}
        if isinstance(enriched_payload, dict):
            constants = enriched_payload.get("c") or {}
            payload_context = enriched_payload
            submission_context = enriched_payload.get("submission") or {}
        context: dict[str, Any] = {
            "field": assertion.target_display or path or "",
            "target": assertion.target_display or path or "",
            "target_field": assertion.target_data_path,
            "target_slug": self._assertion_path(assertion),
            "path": path or "",
            "actual": actual,
            "value": rhs.get("value"),
            "expected": rhs.get("value"),
            "values": rhs.get("values"),
            "min": rhs.get("min"),
            "max": rhs.get("max"),
            "tolerance": rhs.get("tolerance") or options.get("tolerance"),
            "units": options.get("units"),
            "severity": assertion.severity,
            "operator": assertion.get_operator_display(),
            "when": assertion.when_expression,
            "rhs": rhs,
            "options": options,
            "p": payload_context,
            "payload": payload_context,
            "c": constants,
            "const": constants,
            "submission": submission_context,
        }
        self._add_target_alias(context, assertion, actual)
        return context

    def _add_target_alias(
        self,
        context: dict[str, Any],
        assertion: RulesetAssertion,
        actual: Any,
    ) -> None:
        """Add a target alias to the context based on the assertion target."""
        alias = ""
        if assertion.target_signal_definition_id:
            alias = assertion.target_signal_definition.contract_key or ""
        elif assertion.target_data_path:
            alias = assertion.target_data_path
        alias = alias.strip()
        if not alias:
            return
        last_segment = alias
        for sep in (".", "["):
            if sep in last_segment:
                last_segment = re.split(r"[.\[]", alias)[-1]
                break
        last_segment = re.sub(r"]+$", "", last_segment)
        sanitized = re.sub(r"\W+", "_", last_segment).strip("_")
        if sanitized and sanitized not in context:
            context[sanitized] = actual
