"""
CEL expression assertion evaluator.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _

from validibot.validations.assertions.evaluators.registry import register_evaluator
from validibot.validations.assertions.message_templates import (
    MessageTemplateRenderError,
)
from validibot.validations.assertions.message_templates import MessageValueDisplay
from validibot.validations.assertions.message_templates import (
    format_assertion_message_value,
)
from validibot.validations.assertions.message_templates import (
    render_assertion_message_template,
)
from validibot.validations.cel_eval import evaluate_cel_expression
from validibot.validations.constants import CEL_MAX_CONTEXT_SYMBOLS
from validibot.validations.constants import CEL_MAX_EVAL_TIMEOUT_MS
from validibot.validations.constants import CEL_MAX_EXPRESSION_CHARS
from validibot.validations.constants import AssertionType
from validibot.validations.constants import StepIODirection
from validibot.validations.validators.base import ValidationIssue

if TYPE_CHECKING:
    from validibot.validations.assertions.evaluators.base import AssertionContext
    from validibot.validations.models import RulesetAssertion
    from validibot.validations.models import StepIODefinition
    from validibot.validations.models import Validator

logger = logging.getLogger(__name__)

_CEL_NUMBER_PATTERN = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_CEL_IO_REFERENCE_PATTERN = (
    r"(?P<namespace>o|output|i|input)\."
    r"(?P<contract_key>[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
)
_SIMPLE_REFERENCE_COMPARISON = re.compile(
    rf"^(?P<reference>{_CEL_IO_REFERENCE_PATTERN})\s*"
    rf"(?P<operator><=|>=|==|!=|<|>)\s*"
    rf"(?P<literal>{_CEL_NUMBER_PATTERN})$",
)
_SIMPLE_REVERSED_COMPARISON = re.compile(
    rf"^(?P<literal>{_CEL_NUMBER_PATTERN})\s*"
    rf"(?P<operator><=|>=|==|!=|<|>)\s*"
    rf"(?P<reference>{_CEL_IO_REFERENCE_PATTERN})$",
)
_REVERSED_OPERATOR = {
    "<": ">",
    "<=": ">=",
    ">": "<",
    ">=": "<=",
    "==": "==",
    "!=": "!=",
}


@dataclass(frozen=True)
class _SimpleQuantityComparison:
    """One direct step-I/O-to-number comparison with known display metadata."""

    reference: str
    namespace: str
    contract_key: str
    operator: str
    actual: Any
    expected: int | float
    literal_text: str
    io_definition: StepIODefinition

    @property
    def display(self) -> MessageValueDisplay:
        """Return the unit and precision declared by the step I/O contract."""

        metadata = self.io_definition.metadata or {}
        return MessageValueDisplay(
            unit=self.io_definition.unit or "",
            precision=metadata.get("precision"),
        )

    @property
    def label(self) -> str:
        """Return a label without a duplicate trailing unit suffix."""

        label = self.io_definition.label or self.contract_key.replace("_", " ").title()
        unit = (self.io_definition.unit or "").strip()
        suffix = f" ({unit})" if unit else ""
        if suffix and label.endswith(suffix):
            return label[: -len(suffix)]
        return label


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
                now=context.now,
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

        # Evaluate the main expression. now=context.now pins CEL now() to the
        # run clock so a saved time-relative assertion evaluates deterministically
        # instead of failing on an unbound now() (see AssertionContext.now).
        result = evaluate_cel_expression(
            expression=expr,
            context=cel_context,
            timeout_ms=CEL_MAX_EVAL_TIMEOUT_MS,
            now=context.now,
        )

        if not result.success:
            # Expression evaluation failed
            msg = self._format_error_message(
                str(result.error),
                validator=context.validator,
            )
            return [
                self._issue_from_assertion(
                    assertion,
                    path="",
                    message=_("CEL evaluation failed: %(err)s") % {"err": msg},
                ),
            ]

        template_context = self._build_template_context(
            assertion=assertion,
            cel_context=cel_context,
            expr=expr,
            when_expr=when_expr,
        )
        comparison = self._simple_quantity_comparison(
            assertion=assertion,
            context=context,
            cel_context=cel_context,
            expr=expr,
        )
        value_displays = self._add_comparison_template_values(
            template_context=template_context,
            comparison=comparison,
        )

        if not bool(result.value):
            # Expression evaluated to false - assertion failed
            failure_message = self._render_failure_message(
                assertion=assertion,
                template_context=template_context,
                comparison=comparison,
                value_displays=value_displays,
            )
            return [
                self._issue_from_assertion(
                    assertion,
                    path="",
                    message=failure_message,
                ),
            ]

        # Assertion passed - emit success issue if configured
        success_issue = context.engine._maybe_success_issue(
            assertion,
            template_context=template_context,
            value_displays=value_displays,
        )
        if success_issue:
            return [success_issue]

        return []

    def _build_template_context(
        self,
        *,
        assertion: RulesetAssertion,
        cel_context: dict[str, Any],
        expr: str,
        when_expr: str,
    ) -> dict[str, Any]:
        """Build the message-template context for CEL success/failure findings."""
        template_context = dict(cel_context)
        template_context.update(
            {
                "expr": expr,
                "when": when_expr,
                "rhs": assertion.rhs or {},
                "options": assertion.options or {},
                "severity": assertion.severity,
            },
        )
        return template_context

    def _render_failure_message(
        self,
        *,
        assertion: RulesetAssertion,
        template_context: dict[str, Any],
        comparison: _SimpleQuantityComparison | None,
        value_displays: dict[str, MessageValueDisplay],
    ) -> str:
        """Render the configured CEL failure message against the CEL context."""
        template = (assertion.message_template or "").strip()
        if not template:
            if comparison is not None:
                display = comparison.display
                return _(
                    "%(label)s was %(actual)s; expected %(operator)s %(expected)s."
                ) % {
                    "label": comparison.label,
                    "actual": format_assertion_message_value(
                        comparison.actual,
                        display,
                    ),
                    "operator": comparison.operator,
                    "expected": format_assertion_message_value(
                        comparison.expected,
                        display,
                    ),
                }
            return _("CEL assertion evaluated to false.")

        template = self._annotate_literal_threshold(template, comparison)
        try:
            rendered = render_assertion_message_template(
                template,
                template_context,
                value_displays=value_displays,
            )
        except MessageTemplateRenderError:
            return template
        return rendered or template

    def _simple_quantity_comparison(
        self,
        *,
        assertion: RulesetAssertion,
        context: AssertionContext,
        cel_context: dict[str, Any],
        expr: str,
    ) -> _SimpleQuantityComparison | None:
        """Describe a direct measured-value comparison, without guessing.

        Unit propagation is intentionally limited to one declared ``i.*`` or
        ``o.*`` value compared directly with a numeric literal.  Arithmetic,
        helper calls, compound expressions, signals, and cross-step references
        may change dimensions or mix unit systems, so they retain the author's
        message exactly as before.
        """

        normalized = self._strip_outer_parentheses(expr.strip())
        match = _SIMPLE_REFERENCE_COMPARISON.fullmatch(normalized)
        reversed_operands = False
        if match is None:
            match = _SIMPLE_REVERSED_COMPARISON.fullmatch(normalized)
            reversed_operands = match is not None
        if match is None:
            return None

        namespace = match.group("namespace")
        contract_key = match.group("contract_key")
        direction = (
            StepIODirection.OUTPUT
            if namespace in {"o", "output"}
            else StepIODirection.INPUT
        )
        io_definition = getattr(assertion, "target_io_definition", None)
        if not (
            io_definition
            and io_definition.direction == direction
            and io_definition.contract_key == contract_key
        ):
            io_definition = context.get_io_definition(
                direction=direction,
                contract_key=contract_key,
            )
        if io_definition is None or not (io_definition.unit or "").strip():
            return None

        reference = match.group("reference")
        actual, found = self._resolve_template_path(cel_context, reference)
        if not found:
            return None

        operator = match.group("operator")
        if reversed_operands:
            operator = _REVERSED_OPERATOR[operator]
        return _SimpleQuantityComparison(
            reference=reference,
            namespace=namespace,
            contract_key=contract_key,
            operator=operator,
            actual=actual,
            expected=self._number_literal(match.group("literal")),
            literal_text=match.group("literal"),
            io_definition=io_definition,
        )

    @staticmethod
    def _annotate_literal_threshold(
        template: str,
        comparison: _SimpleQuantityComparison | None,
    ) -> str:
        """Add the known unit to an authored copy of the CEL threshold.

        Existing messages often interpolate the actual namespace value but
        repeat the numeric threshold as plain text.  For a direct comparison,
        that literal necessarily has the target quantity's unit.  Restricting
        the rewrite to templates that also interpolate that same target avoids
        treating unrelated numbers in arbitrary prose as measured values.
        """

        if comparison is None:
            return template

        short_namespace = "o" if comparison.namespace in {"o", "output"} else "i"
        long_namespace = "output" if short_namespace == "o" else "input"
        target_placeholders = {
            "actual",
            f"{short_namespace}.{comparison.contract_key}",
            f"{long_namespace}.{comparison.contract_key}",
        }
        placeholders = {
            match.group("expr").split("|", 1)[0].strip()
            for match in re.finditer(r"{{\s*(?P<expr>.*?)\s*}}", template)
        }
        if not placeholders.intersection(target_placeholders):
            return template
        if placeholders.intersection({"expected", "value"}):
            return template

        unit = comparison.display.unit.strip()
        literal_pattern = re.compile(
            rf"(?<![\w.]){re.escape(comparison.literal_text)}(?![\w.])"
        )

        def _annotate_plain_text(text: str) -> str:
            def _with_unit(match: re.Match) -> str:
                following = text[match.end() :].lstrip()
                if following.startswith(unit):
                    return match.group(0)
                return format_assertion_message_value(
                    comparison.expected,
                    comparison.display,
                )

            return literal_pattern.sub(_with_unit, text)

        parts = re.split(r"({{\s*.*?\s*}})", template)
        return "".join(
            part if part.startswith("{{") else _annotate_plain_text(part)
            for part in parts
        )

    def _add_comparison_template_values(
        self,
        *,
        template_context: dict[str, Any],
        comparison: _SimpleQuantityComparison | None,
    ) -> dict[str, MessageValueDisplay]:
        """Expose conventional values and display metadata to message templates."""

        if comparison is None:
            return {}

        template_context.update(
            {
                "actual": comparison.actual,
                "expected": comparison.expected,
                "value": comparison.expected,
                "units": comparison.display.unit,
                "target": comparison.label,
                "comparison_operator": comparison.operator,
            },
        )
        short_namespace = "o" if comparison.namespace in {"o", "output"} else "i"
        long_namespace = "output" if short_namespace == "o" else "input"
        display = comparison.display
        return {
            comparison.reference: display,
            f"{short_namespace}.{comparison.contract_key}": display,
            f"{long_namespace}.{comparison.contract_key}": display,
            "actual": display,
            "expected": display,
            "value": display,
        }

    @staticmethod
    def _strip_outer_parentheses(expression: str) -> str:
        """Strip only parentheses that enclose the complete expression."""

        value = expression
        while value.startswith("(") and value.endswith(")"):
            depth = 0
            encloses_all = True
            for index, char in enumerate(value):
                if char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0 and index != len(value) - 1:
                        encloses_all = False
                        break
            if not encloses_all or depth != 0:
                break
            value = value[1:-1].strip()
        return value

    @staticmethod
    def _number_literal(value: str) -> int | float:
        """Convert a validated CEL numeric literal for display."""

        return (
            float(value)
            if any(marker in value.lower() for marker in (".", "e"))
            else int(value)
        )

    @staticmethod
    def _resolve_template_path(
        cel_context: dict[str, Any],
        reference: str,
    ) -> tuple[Any, bool]:
        """Resolve the comparison target through the shared path grammar."""

        from validibot.validations.services.path_resolution import resolve_path

        return resolve_path(cel_context, reference)

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

    def _format_error_message(
        self,
        raw_error: str,
        validator: Validator | None = None,
    ) -> str:
        """Format CEL error messages for better user readability.

        Handles three error patterns:

        1. **Dot-notation with @** — the user wrote ``m.@Conductivity``
           which is a CEL syntax error.  Suggest bracket notation instead.
        2. **Field selection failure** — the user wrote ``m.Conductivity``
           but the XML-derived dict has ``@Conductivity``.  Suggest ``@``.
        3. **Undefined identifier** — the expression references a name
           that isn't in the CEL context.  Guidance varies by validator.
        """
        # --- Pattern 1: dot-notation with @ (compile error) ----------------
        if ".@" in raw_error or ".@" in (
            raw_error.split("\n", maxsplit=1)[0] if raw_error else ""
        ):
            return _(
                "The '@' character cannot be used with dot notation in CEL. "
                "Use bracket notation for XML attributes — for example, "
                'm["@Conductivity"] instead of m.@Conductivity.'
            )

        # --- Pattern 2: field-selection failure (XML @-attribute) ----------
        if "does not support field selection" in raw_error:
            return _(
                "A field in the expression was not found. "
                "XML attributes require an '@' prefix — for example, "
                'use m["@Conductivity"] or double(m["@Conductivity"]) '
                "instead of m.Conductivity."
            )

        # --- Pattern 3: missing map member (e.g. m.Conductivity when key
        # is actually @Conductivity) — with CEL MapType conversion, this
        # produces "no such member in mapping" instead of the older
        # "does not support field selection" error.  The quotes in the
        # error string are often backslash-escaped, so we match flexibly.
        missing_member = re.search(
            r"no such member in mapping:\s*\\*['\"]?(?P<name>\w+)\\*['\"]?",
            raw_error,
        )
        if missing_member:
            name = missing_member.group("name")
            return _(
                "Field '%(name)s' was not found. If this is an XML "
                "attribute, use the '@' prefix with bracket notation: "
                'm["@%(name)s"] instead of m.%(name)s.'
            ) % {"name": name}

        # --- Pattern 4: undefined identifier -------------------------------
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
            allows_custom = getattr(validator, "allow_custom_assertion_targets", False)
            if allows_custom:
                return _(
                    "CEL references undefined name '%(identifier)s'. "
                    "Check that this data path exists in the submission."
                ) % {"identifier": identifier}
            return _(
                "CEL references undefined name '%(identifier)s'. "
                "Ensure a matching step input or output exists."
            ) % {"identifier": identifier}

        return raw_error
