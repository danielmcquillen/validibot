"""Shared rendering for assertion finding message templates.

Assertion messages are persisted as plain strings on findings, but authors can
insert run-time values with a small ``{{ name }}`` syntax. Keep this renderer
independent of Django templates: the supported surface is intentionally tiny,
deterministic, and valid in workers that are not rendering HTML.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from validibot.validations.services.path_resolution import resolve_path

logger = logging.getLogger(__name__)

_TEMPLATE_PATTERN = re.compile(r"{{\s*(?P<expr>.*?)\s*}}")
_FILTER_PATTERN = re.compile(r"^(?P<name>\w+)(?:\((?P<args>.*)\))?$")


class MessageTemplateRenderError(Exception):
    """Raised when an assertion message template fails to render."""


def render_assertion_message_template(template: str, context: dict[str, Any]) -> str:
    """Render an assertion message template with variables and simple filters.

    Variables resolve either as flat keys (``{{ actual }}``) or as
    dotted/bracket paths into namespace dictionaries (``{{ c.energy_price }}``,
    ``{{ p.items[0].price }}``). Unknown values are left in place unless a filter
    such as ``default("fallback")`` supplies a replacement.
    """

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
        return str(value)

    return _TEMPLATE_PATTERN.sub(_replace, template)


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
