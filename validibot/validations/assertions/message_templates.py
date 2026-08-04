"""Shared rendering for assertion finding message templates.

Assertion messages are persisted as plain strings on findings, but authors can
insert run-time values with a small ``{{ name }}`` syntax. Keep this renderer
independent of Django templates: the supported surface is intentionally tiny,
deterministic, and valid in workers that are not rendering HTML.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from validibot.validations.services.path_resolution import resolve_path

logger = logging.getLogger(__name__)

_TEMPLATE_PATTERN = re.compile(r"{{\s*(?P<expr>.*?)\s*}}")
_FILTER_PATTERN = re.compile(r"^(?P<name>\w+)(?:\((?P<args>.*)\))?$")


class MessageTemplateRenderError(Exception):
    """Raised when an assertion message template fails to render."""


@dataclass(frozen=True)
class MessageValueDisplay:
    """Display metadata for one assertion-message template value.

    Evaluation contexts must keep raw numeric values so CEL and BASIC comparisons
    remain type-correct.  Message rendering is the presentation boundary where a
    known unit and catalog precision can safely be applied.
    """

    unit: str = ""
    precision: int | None = None


def render_assertion_message_template(
    template: str,
    context: dict[str, Any],
    *,
    value_displays: dict[str, MessageValueDisplay] | None = None,
) -> str:
    """Render an assertion message template with variables and simple filters.

    Variables resolve either as flat keys (``{{ actual }}``) or as
    dotted/bracket paths into namespace dictionaries (``{{ c.energy_price }}``,
    ``{{ p.items[0].price }}``). Unknown values are left in place unless a filter
    such as ``default("fallback")`` supplies a replacement.  Callers may provide
    display metadata for selected variables; numeric values at those paths are
    then formatted with catalog precision and units after template filters run.
    """

    displays = value_displays or {}

    def _replace(match: re.Match) -> str:
        expr = match.group("expr")
        try:
            value = resolve_template_expression(expr, context)
        except Exception as exc:
            logger.exception(
                "Failed to render assertion message template expression '%s'.",
                expr,
            )
            raise MessageTemplateRenderError from exc
        if value is None:
            return match.group(0)
        display = displays.get(_template_value_key(expr))
        if display is not None:
            value = format_assertion_message_value(
                value,
                display,
                precision_override=_round_filter_precision(expr),
            )
        return str(value)

    return _TEMPLATE_PATTERN.sub(_replace, template)


def format_assertion_message_value(
    value: Any,
    display: MessageValueDisplay,
    *,
    precision_override: int | None = None,
) -> str:
    """Format one known quantity for a human-readable assertion finding.

    Integers remain integral, while floating-point and Decimal values use the
    catalog precision or two decimal places by default.  Non-numeric values are
    left unchanged apart from an optional unit suffix; callers only attach this
    metadata where the step I/O contract identifies a measured quantity.
    """

    formatted: str
    if isinstance(value, bool):
        formatted = str(value)
    elif isinstance(value, int):
        formatted = f"{value:,}"
    elif isinstance(value, (float, Decimal)):
        precision = _normalized_precision(
            precision_override if precision_override is not None else display.precision,
        )
        formatted = f"{value:,.{precision}f}"
    else:
        formatted = str(value)

    unit = (display.unit or "").strip()
    return f"{formatted} {unit}" if unit else formatted


def _template_value_key(expr: str) -> str:
    """Return the unfiltered variable path from one template expression."""

    return expr.split("|", 1)[0].strip()


def _round_filter_precision(expr: str) -> int | None:
    """Return an explicit ``round`` filter precision, when one is present."""

    for raw_filter in expr.split("|")[1:]:
        match = _FILTER_PATTERN.match(raw_filter.strip())
        if not match or match.group("name") != "round":
            continue
        args = _parse_filter_args(match.group("args"))
        if not args:
            return 0
        try:
            return _normalized_precision(int(float(args[0])))
        except (TypeError, ValueError):
            return 0
    return None


def _normalized_precision(value: Any) -> int:
    """Return a bounded display precision, defaulting to two decimals."""

    if value is None:
        return 2
    try:
        precision = int(value)
    except (TypeError, ValueError):
        return 2
    return max(0, min(precision, 12))


def resolve_template_expression(expr: str, context: dict[str, Any]) -> Any:
    """Resolve a template expression with optional filters."""
    parts = [part.strip() for part in expr.split("|") if part.strip()]
    if not parts:
        return ""
    value = _lookup_context_value(parts[0], context)
    for spec in parts[1:]:
        value = _apply_template_filter(value, spec)
    return value


def _lookup_context_value(key: str, context: dict[str, Any]) -> Any:
    """Look up ``key`` as a flat name first, then as a dotted/bracket path."""
    if key in context:
        return context[key]
    value, found = resolve_path(context, key)
    if found:
        return value
    return None


def _apply_template_filter(value: Any, spec: str) -> Any:
    """Apply a supported message-template filter."""
    if spec == "":
        return value
    match = _FILTER_PATTERN.match(spec)
    if not match:
        return value
    name = match.group("name")
    args = _parse_filter_args(match.group("args"))
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


def _parse_filter_args(args: str | None) -> list[str]:
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
