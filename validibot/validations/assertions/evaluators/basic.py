"""
BASIC assertion evaluator.

This evaluator handles BASIC assertions with operator dispatch to specialized
methods for each operator type (equality, comparison, membership, string ops, etc.).
"""

from __future__ import annotations

import concurrent.futures
import logging
import re
from typing import TYPE_CHECKING
from typing import Any

from django.utils.html import strip_tags
from django.utils.translation import gettext as _

from validibot.validations.assertions.evaluators.registry import register_evaluator
from validibot.validations.constants import REGEX_EVAL_TIMEOUT_MS
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.validators.base import ValidationIssue

if TYPE_CHECKING:
    from validibot.validations.assertions.evaluators.base import AssertionContext
    from validibot.validations.models import RulesetAssertion

logger = logging.getLogger(__name__)

_PATH_TOKEN_PATTERN = re.compile(r"([A-Za-z0-9_-]+)|\[(\d+)\]")
_TEMPLATE_PATTERN = re.compile(r"{{\s*(?P<expr>.*?)\s*}}")
_FILTER_PATTERN = re.compile(r"^(?P<name>\w+)(?:\((?P<args>.*)\))?$")


class MessageTemplateRenderError(Exception):
    """Raised when an assertion message template fails to render."""


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
        actual, found = self._resolve_path(payload, path)
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
        success_issue = context.engine._maybe_success_issue(assertion)
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
        """Get the target path for an assertion."""
        if assertion.target_catalog_entry_id and assertion.target_catalog_entry:
            return assertion.target_catalog_entry.slug
        return assertion.target_field

    def _resolve_path(self, data: Any, path: str | None) -> tuple[Any, bool]:
        """
        Resolve a dot/bracket path in the data.

        Args:
            data: The payload to navigate.
            path: Path like "foo.bar[0].baz".

        Returns:
            Tuple of (resolved_value, was_found).
        """
        if not path:
            return data, True
        current = data
        for match in _PATH_TOKEN_PATTERN.finditer(path):
            key, index = match.groups()
            if key:
                if isinstance(current, dict) and key in current:
                    current = current[key]
                else:
                    return None, False
            elif index is not None:
                position = int(index)
                if isinstance(current, (list, tuple)) and 0 <= position < len(current):
                    current = current[position]
                else:
                    return None, False
        return current, True

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
        flags = re.IGNORECASE if options.get("case_insensitive") else 0
        try:
            passed = self._run_regex_with_timeout(pattern, actual_text, flags)
        except concurrent.futures.TimeoutError:
            return False, _("Regex evaluation timed out (possible ReDoS pattern).")
        except re.error as exc:
            return False, _("Invalid regex: %(error)s") % {"error": exc}
        return passed, _("Regex comparison failed.")

    @staticmethod
    def _run_regex_with_timeout(
        pattern: str,
        text: str,
        flags: int,
    ) -> bool:
        """Run re.search with a timeout to prevent ReDoS attacks."""
        timeout_secs = REGEX_EVAL_TIMEOUT_MS / 1000.0
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(re.search, pattern, text, flags)
            return future.result(timeout=timeout_secs) is not None

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
                rendered = self._render_message_template(template, context)
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
    ) -> dict[str, Any]:
        """Build a context dict for message template rendering."""
        context: dict[str, Any] = {
            "field": assertion.target_display or path or "",
            "target": assertion.target_display or path or "",
            "target_field": assertion.target_field,
            "target_slug": getattr(assertion.target_catalog_entry, "slug", "")
            if assertion.target_catalog_entry_id
            else "",
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
        if assertion.target_catalog_entry_id and assertion.target_catalog_entry:
            alias = assertion.target_catalog_entry.slug or ""
        elif assertion.target_field:
            alias = assertion.target_field
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

    def _render_message_template(
        self,
        template: str,
        context: dict[str, Any],
    ) -> str:
        """Render a message template with variable substitution and filters."""

        def _replace(match: re.Match) -> str:
            expr = match.group("expr")
            try:
                value = self._resolve_template_expression(expr, context)
            except Exception as exc:
                logger.exception(
                    "Failed to render assertion message template expression '%s'.",
                    expr,
                )
                raise MessageTemplateRenderError from exc
            if value is None:
                return match.group(0)
            return str(value)

        return _TEMPLATE_PATTERN.sub(_replace, template)

    def _resolve_template_expression(
        self,
        expr: str,
        context: dict[str, Any],
    ) -> Any:
        """Resolve a template expression with optional filters."""
        parts = [part.strip() for part in expr.split("|") if part.strip()]
        if not parts:
            return ""
        key = parts[0]
        value = context.get(key)
        for spec in parts[1:]:
            value = self._apply_template_filter(value, spec)
        return value

    def _apply_template_filter(self, value: Any, spec: str) -> Any:
        """Apply a template filter to a value."""
        if spec == "":
            return value
        match = _FILTER_PATTERN.match(spec)
        if not match:
            return value
        name = match.group("name")
        args = self._parse_filter_args(match.group("args"))
        if name == "round":
            digits = 0
            if args:
                try:
                    digits = int(float(args[0]))
                except (TypeError, ValueError):
                    digits = 0
            try:
                number = float(value)
            except (TypeError, ValueError):
                return value
            rounded = round(number, digits)
            if digits == 0:
                if rounded.is_integer():
                    return int(rounded)
                return rounded
            return rounded
        if name == "upper":
            return str(value).upper()
        if name == "lower":
            return str(value).lower()
        if name == "default":
            return value if value not in (None, "") else (args[0] if args else "")
        return value

    def _parse_filter_args(self, args: str | None) -> list[str]:
        """Parse filter arguments from a filter specification."""
        if not args:
            return []
        parsed: list[str] = []
        for raw in args.split(","):
            val = raw.strip()
            if len(val) >= 2 and val[0] in {'"', "'"} and val[-1] == val[0]:  # noqa: PLR2004
                val = val[1:-1]
            parsed.append(val)
        return parsed
