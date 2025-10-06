from __future__ import annotations

import math
import re
from dataclasses import dataclass
from statistics import mean
from statistics import pstdev
from typing import Any

from django.utils.translation import gettext_lazy as _

from simplevalidations.validations.constants import Severity
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.engines.base import BaseValidatorEngine
from simplevalidations.validations.engines.base import ValidationIssue
from simplevalidations.validations.engines.base import ValidationResult
from simplevalidations.validations.engines.registry import register_engine


@dataclass(slots=True)
class PolicyRule:
    identifier: str
    path: str
    operator: str
    value: Any
    value_b: Any | None
    message: str


def _ensure_json_structure(raw: str) -> Any:
    import json

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _flatten_path_tokens(path: str) -> list[str]:
    cleaned = path.strip()
    if cleaned.startswith("$"):
        cleaned = cleaned[1:]
    cleaned = cleaned.lstrip(".")
    if not cleaned:
        return []
    tokens: list[str] = []
    buffer = ""
    i = 0
    while i < len(cleaned):
        ch = cleaned[i]
        if ch == "[":
            if buffer:
                tokens.append(buffer)
                buffer = ""
            end = cleaned.find("]", i)
            if end == -1:
                tokens.append(cleaned[i:])
                break
            tokens.append(cleaned[i : end + 1])
            i = end + 1
            continue
        if ch == ".":
            if buffer:
                tokens.append(buffer)
                buffer = ""
            i += 1
            continue
        buffer += ch
        i += 1
    if buffer:
        tokens.append(buffer)
    return [token for token in tokens if token]


def _resolve_path(data: Any, path: str) -> list[tuple[str, Any]]:
    tokens = _flatten_path_tokens(path)
    if not tokens:
        return [("$", data)]

    results: list[tuple[str, Any]] = [("$", data)]
    for token in tokens:
        next_results: list[tuple[str, Any]] = []
        is_wildcard = token in {"*", "[*]"}
        index_match = re.fullmatch(r"\[(\d+)\]", token)
        for current_path, current_value in results:
            if isinstance(current_value, dict):
                if is_wildcard:
                    for key, value in current_value.items():
                        next_results.append((f"{current_path}.{key}", value))
                elif token in current_value:
                    next_results.append(
                        (f"{current_path}.{token}", current_value[token])
                    )
            if isinstance(current_value, list):
                if is_wildcard:
                    for idx, value in enumerate(current_value):
                        next_results.append((f"{current_path}[{idx}]", value))
                elif index_match:
                    idx = int(index_match.group(1))
                    if 0 <= idx < len(current_value):
                        next_results.append(
                            (f"{current_path}[{idx}]", current_value[idx])
                        )
        results = next_results
        if not results:
            break
    return results


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _compare(rule: PolicyRule, actual: Any) -> tuple[str, Severity, str]:
    operator = rule.operator
    if operator in {">=", ">", "<", "<=", "==", "!="}:
        expected_num = _coerce_number(rule.value)
        actual_num = _coerce_number(actual)
        if expected_num is None or actual_num is None:
            return (
                "UNKNOWN",
                Severity.WARNING,
                _("Cannot compare non-numeric value for rule '%(rule)s'.")
                % {"rule": rule.identifier},
            )
        match operator:
            case ">=":
                ok = actual_num >= expected_num
            case ">":
                ok = actual_num > expected_num
            case "<":
                ok = actual_num < expected_num
            case "<=":
                ok = actual_num <= expected_num
            case "==":
                ok = math.isclose(actual_num, expected_num, rel_tol=0.0, abs_tol=1e-9)
            case "!=":
                ok = not math.isclose(
                    actual_num, expected_num, rel_tol=0.0, abs_tol=1e-9
                )
            case _:
                ok = False
        if ok:
            return (
                "YES",
                Severity.INFO,
                _("Value meets policy for rule '%(rule)s'.")
                % {"rule": rule.identifier},
            )
        severity = (
            Severity.ERROR if operator in {">=", "<=", ">", "<"} else Severity.WARNING
        )
        return "NO", severity, rule.message
    if operator == "between":
        if rule.value is None or rule.value_b is None:
            return (
                "UNKNOWN",
                Severity.WARNING,
                _("Rule '%(rule)s' missing bounds for 'between'.")
                % {"rule": rule.identifier},
            )
        lower = _coerce_number(rule.value)
        upper = _coerce_number(rule.value_b)
        actual_num = _coerce_number(actual)
        if lower is None or upper is None or actual_num is None:
            return (
                "UNKNOWN",
                Severity.WARNING,
                _("Cannot evaluate 'between' for rule '%(rule)s'.")
                % {"rule": rule.identifier},
            )
        ok = lower <= actual_num <= upper
        if ok:
            return (
                "YES",
                Severity.INFO,
                _("Value within expected range for rule '%(rule)s'.")
                % {"rule": rule.identifier},
            )
        return "NO", Severity.ERROR, rule.message
    if operator in {"in", "not_in"}:
        options: set[str] = set()
        value = rule.value
        if isinstance(value, (list, tuple)):
            options = {str(v).strip() for v in value}
        elif isinstance(value, str):
            options = {part.strip() for part in value.split(",") if part.strip()}
        target = str(actual).strip()
        if operator == "in":
            ok = target in options
            if ok:
                return (
                    "YES",
                    Severity.INFO,
                    _("Value allowed for rule '%(rule)s'.") % {"rule": rule.identifier},
                )
            return "NO", Severity.ERROR, rule.message
        ok = target not in options
        if ok:
            return (
                "YES",
                Severity.INFO,
                _("Value allowed for rule '%(rule)s'.") % {"rule": rule.identifier},
            )
        return "NO", Severity.ERROR, rule.message
    if operator == "nonempty":
        if actual in ({}, [], "", None):
            return "NO", Severity.ERROR, rule.message
        return (
            "YES",
            Severity.INFO,
            _("Value present for rule '%(rule)s'.") % {"rule": rule.identifier},
        )
    return (
        "UNKNOWN",
        Severity.WARNING,
        _("Unsupported operator '%(operator)s' for rule '%(rule)s'.")
        % {"operator": operator, "rule": rule.identifier},
    )


def _build_issue(
    rule: PolicyRule, status: str, severity: Severity, path: str, detail: str
) -> ValidationIssue | None:
    if status == "YES":
        return None
    message = rule.message or detail
    code = f"AI_RULE_{rule.identifier.upper()}"
    return ValidationIssue(
        path=path or rule.path, message=f"{message} ({status})", severity=severity
    )


def _heuristic_critiques(data: Any) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    def walk(node: Any, pointer: str = "$") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                next_pointer = f"{pointer}.{key}" if pointer != "$" else f"$.{key}"
                walk(value, next_pointer)
        elif isinstance(node, list):
            if node and all(
                isinstance(v, (int, float)) for v in node if not isinstance(v, bool)
            ):
                numeric_values = [
                    float(v)
                    for v in node
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                ]
                if numeric_values:
                    avg = mean(numeric_values)
                    stdev = pstdev(numeric_values) if len(numeric_values) > 1 else 0.0
                    if len(numeric_values) >= 24 and avg > 0.8:
                        issues.append(
                            ValidationIssue(
                                path=pointer,
                                message=_(
                                    "Schedule appears to run 24/7 (average %.2f)."
                                )
                                % avg,
                                severity=Severity.WARNING,
                            )
                        )
                    if (
                        stdev == 0
                        and len(set(numeric_values)) == 1
                        and len(numeric_values) > 4
                    ):
                        issues.append(
                            ValidationIssue(
                                path=pointer,
                                message=_(
                                    "Numeric series is flat; check if variation was expected."
                                ),
                                severity=Severity.INFO,
                            )
                        )
                    for idx, value in enumerate(numeric_values):
                        if value < 0:
                            issues.append(
                                ValidationIssue(
                                    path=f"{pointer}[{idx}]",
                                    message=_(
                                        "Negative value %(value)s detected in schedule."
                                    )
                                    % {"value": value},
                                    severity=Severity.WARNING,
                                )
                            )
            seen = set()
            for idx, value in enumerate(node):
                next_pointer = f"{pointer}[{idx}]"
                if isinstance(value, (int, float, str)):
                    if value in seen:
                        issues.append(
                            ValidationIssue(
                                path=next_pointer,
                                message=_(
                                    "Duplicate value '%(value)s' detected in list."
                                    % {"value": value}
                                ),
                                severity=Severity.INFO,
                            )
                        )
                    else:
                        seen.add(value)
                walk(value, next_pointer)
        else:
            if isinstance(node, (int, float)) and not isinstance(node, bool):
                if node < 0:
                    issues.append(
                        ValidationIssue(
                            path=pointer,
                            message=_("Negative numeric value %(value)s detected.")
                            % {"value": node},
                            severity=Severity.WARNING,
                        )
                    )
                key_lower = pointer.lower()
                if any(token in key_lower for token in ["setpoint", "temperature"]):
                    if node < 15 or node > 40:
                        issues.append(
                            ValidationIssue(
                                path=pointer,
                                message=_(
                                    "Temperature %(value)s°C appears outside a realistic comfort band."
                                )
                                % {"value": node},
                                severity=Severity.WARNING,
                            )
                        )
                if any(token in key_lower for token in ["load", "power", "capacity"]):
                    if abs(node) > 1_000_000:
                        issues.append(
                            ValidationIssue(
                                path=pointer,
                                message=_(
                                    "Value %(value)s looks unusually large; verify units."
                                )
                                % {"value": node},
                                severity=Severity.WARNING,
                            )
                        )
            if isinstance(node, str):
                if len(node) > 80 and node == node.upper():
                    issues.append(
                        ValidationIssue(
                            path=pointer,
                            message=_(
                                "Long uppercase string detected; confirm this is intentional."
                            ),
                            severity=Severity.INFO,
                        )
                    )

    walk(data)
    return issues


@register_engine(ValidationType.AI_ASSIST)
class AiAssistEngine(BaseValidatorEngine):
    """Heuristic AI-assist validator providing policy checks and critiques."""

    def validate(self, validator, submission, ruleset):
        content = submission.get_content()
        parsed = _ensure_json_structure(content)
        issues: list[ValidationIssue] = []
        stats: dict[str, Any] = {}

        if parsed is None:
            issues.append(
                ValidationIssue(
                    path="$",
                    message=_(
                        "AI assist currently supports JSON submissions. Unable to parse this document."
                    ),
                    severity=Severity.WARNING,
                )
            )
            return ValidationResult(
                passed=False, issues=issues, stats={"ai_template": "unparsed"}
            )

        config = self.config or {}
        template = config.get("template", "ai_critic")
        selectors = config.get("selectors", [])
        rules_raw = config.get("policy_rules", [])
        mode = config.get("mode", "ADVISORY").upper()
        cost_cap = config.get("cost_cap_cents", 10)

        stats.update(
            {
                "ai_template": template,
                "ai_mode": mode,
                "ai_cost_cap_cents": cost_cap,
                "ai_selector_count": len(selectors or []),
                "ai_policy_rule_count": len(rules_raw or []),
            }
        )

        if selectors:
            snippets: dict[str, list[Any]] = {}
            for selector in selectors:
                matches = _resolve_path(parsed, selector)
                snippets[selector] = [value for _path, value in matches][:5]
            stats["ai_selector_samples"] = snippets

        issues.extend(_heuristic_critiques(parsed))

        if rules_raw:
            for rule_data in rules_raw:
                rule = PolicyRule(
                    identifier=rule_data.get("id", "rule"),
                    path=rule_data.get("path", "$"),
                    operator=rule_data.get("operator", "unknown"),
                    value=rule_data.get("value"),
                    value_b=rule_data.get("value_b"),
                    message=rule_data.get("message")
                    or _("Policy requirement not met."),
                )
                matches = _resolve_path(parsed, rule.path)
                if not matches:
                    issue = ValidationIssue(
                        path=rule.path,
                        message=_("No data found for policy rule '%(rule)s'.")
                        % {"rule": rule.identifier},
                        severity=Severity.WARNING,
                    )
                    issues.append(issue)
                    continue
                for match_path, value in matches:
                    status, severity, detail = _compare(rule, value)
                    if mode == "BLOCKING" and severity == Severity.ERROR:
                        severity = Severity.ERROR
                    issue = _build_issue(rule, status, severity, match_path, detail)
                    if issue:
                        if mode == "BLOCKING" and issue.severity != Severity.INFO:
                            issue.severity = Severity.ERROR
                        issues.append(issue)

        has_error = any(issue.severity == Severity.ERROR for issue in issues)
        result = ValidationResult(passed=not has_error, issues=issues, stats=stats)
        return result
