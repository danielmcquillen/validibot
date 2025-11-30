from __future__ import annotations

import json
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

JSON_POINTER_ROOT = "$"
JSON_POINTER_SEPARATOR = "."
LIST_START_CHAR = "["
LIST_END_CHAR = "]"
LIST_INDEX_TEMPLATE = "[{index}]"
LIST_INDEX_PATTERN = r"\[(\d+)\]"
LIST_INDEX_REGEX = re.compile(LIST_INDEX_PATTERN)
FIND_NOT_FOUND = -1
WILDCARD_TOKENS = frozenset({"*", "[*]"})
COMPARISON_OPERATORS = frozenset({">=", ">", "<", "<=", "==", "!="})
STRICT_COMPARISON_OPERATORS = frozenset({">=", ">", "<", "<="})
BETWEEN_OPERATOR = "between"
IN_OPERATOR = "in"
NOT_IN_OPERATOR = "not_in"
IN_OPERATORS = frozenset({IN_OPERATOR, NOT_IN_OPERATOR})
NONEMPTY_OPERATOR = "nonempty"
VALUE_OPTIONS_DELIMITER = ","
STATUS_PASS = "YES" # noqa: S105
STATUS_FAIL = "NO"
STATUS_UNKNOWN = "UNKNOWN"
NUMERIC_EQUALITY_REL_TOL = 0.0
NUMERIC_EQUALITY_ABS_TOL = 1e-9
ZERO_STD_DEV_THRESHOLD = 0.0
SINGLE_VALUE_VARIATION_COUNT = 1
FLAT_SERIES_MIN_LENGTH = 5
NEGATIVE_THRESHOLD = 0
SCHEDULE_CONTINUOUS_MIN_LENGTH = 24
SCHEDULE_AVERAGE_THRESHOLD = 0.8
PSTDEV_MIN_SAMPLE_SIZE = 2
TEMPERATURE_MIN_C = 15
TEMPERATURE_MAX_C = 40
POWER_ABSOLUTE_THRESHOLD = 1_000_000
UPPERCASE_STRING_THRESHOLD = 80
SELECTOR_SAMPLE_LIMIT = 5
CONFIG_KEY_TEMPLATE = "template"
CONFIG_KEY_SELECTORS = "selectors"
CONFIG_KEY_POLICY_RULES = "policy_rules"
CONFIG_KEY_MODE = "mode"
CONFIG_KEY_COST_CAP = "cost_cap_cents"
DEFAULT_TEMPLATE = "ai_critic"
DEFAULT_SELECTORS: tuple[str, ...] = ()
DEFAULT_POLICY_RULES: tuple[dict[str, Any], ...] = ()
DEFAULT_MODE = "ADVISORY"
MODE_BLOCKING = "BLOCKING"
DEFAULT_COST_CAP_CENTS = 10
DEFAULT_RULE_IDENTIFIER = "rule"
DEFAULT_RULE_PATH = JSON_POINTER_ROOT
DEFAULT_RULE_OPERATOR = "unknown"
DEFAULT_RULE_MESSAGE = _("Policy requirement not met.")
EMPTY_VALUES = ({}, [], "", None)
TEMPERATURE_KEY_TOKENS = ("setpoint", "temperature")
POWER_KEY_TOKENS = ("load", "power", "capacity")
STAT_KEY_TEMPLATE = "ai_template"
STAT_KEY_MODE = "ai_mode"
STAT_KEY_COST_CAP = "ai_cost_cap_cents"
STAT_KEY_SELECTOR_COUNT = "ai_selector_count"
STAT_KEY_POLICY_RULE_COUNT = "ai_policy_rule_count"
STAT_KEY_SELECTOR_SAMPLES = "ai_selector_samples"
UNPARSED_TEMPLATE_NAME = "unparsed"
RULE_DATA_KEY_ID = "id"
RULE_DATA_KEY_PATH = "path"
RULE_DATA_KEY_OPERATOR = "operator"
RULE_DATA_KEY_VALUE = "value"
RULE_DATA_KEY_VALUE_B = "value_b"
RULE_DATA_KEY_MESSAGE = "message"


@dataclass(slots=True)
class PolicyRule:
    identifier: str
    path: str
    operator: str
    value: Any
    value_b: Any | None
    message: str


def _ensure_json_structure(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _flatten_path_tokens(path: str) -> list[str]:
    cleaned = path.strip()
    if cleaned.startswith(JSON_POINTER_ROOT):
        cleaned = cleaned[len(JSON_POINTER_ROOT) :]
    cleaned = cleaned.lstrip(JSON_POINTER_SEPARATOR)
    if not cleaned:
        return []
    tokens: list[str] = []
    buffer = ""
    i = 0
    while i < len(cleaned):
        ch = cleaned[i]
        if ch == LIST_START_CHAR:
            if buffer:
                tokens.append(buffer)
                buffer = ""
            end = cleaned.find(LIST_END_CHAR, i)
            if end == FIND_NOT_FOUND:
                tokens.append(cleaned[i:])
                break
            tokens.append(cleaned[i : end + 1])
            i = end + 1
            continue
        if ch == JSON_POINTER_SEPARATOR:
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
        return [(JSON_POINTER_ROOT, data)]

    results: list[tuple[str, Any]] = [(JSON_POINTER_ROOT, data)]
    for token in tokens:
        next_results: list[tuple[str, Any]] = []
        is_wildcard = token in WILDCARD_TOKENS
        index_match = LIST_INDEX_REGEX.fullmatch(token)
        for current_path, current_value in results:
            if isinstance(current_value, dict):
                if is_wildcard:
                    for key, value in current_value.items():
                        next_results.append(
                            (
                                f"{current_path}{JSON_POINTER_SEPARATOR}{key}",
                                value,
                            ),
                        )
                elif token in current_value:
                    next_results.append(
                        (
                            f"{current_path}{JSON_POINTER_SEPARATOR}{token}",
                            current_value[token],
                        ),
                    )
            if isinstance(current_value, list):
                if is_wildcard:
                    for idx, value in enumerate(current_value):
                        next_results.append(
                            (
                                f"{current_path}{LIST_INDEX_TEMPLATE.format(index=idx)}",
                                value,
                            ),
                        )
                elif index_match:
                    idx = int(index_match.group(1))
                    if 0 <= idx < len(current_value):
                        next_results.append(
                            (
                                f"{current_path}{LIST_INDEX_TEMPLATE.format(index=idx)}",
                                current_value[idx],
                            ),
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
    if operator in COMPARISON_OPERATORS:
        expected_num = _coerce_number(rule.value)
        actual_num = _coerce_number(actual)
        if expected_num is None or actual_num is None:
            return (
                STATUS_UNKNOWN,
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
                ok = math.isclose(
                    actual_num,
                    expected_num,
                    rel_tol=NUMERIC_EQUALITY_REL_TOL,
                    abs_tol=NUMERIC_EQUALITY_ABS_TOL,
                )
            case "!=":
                ok = not math.isclose(
                    actual_num,
                    expected_num,
                    rel_tol=NUMERIC_EQUALITY_REL_TOL,
                    abs_tol=NUMERIC_EQUALITY_ABS_TOL,
                )
            case _:
                ok = False
        if ok:
            return (
                STATUS_PASS,
                Severity.INFO,
                _("Value meets policy for rule '%(rule)s'.")
                % {"rule": rule.identifier},
            )
        severity = (
            Severity.ERROR
            if operator in STRICT_COMPARISON_OPERATORS
            else Severity.WARNING
        )
        return STATUS_FAIL, severity, rule.message
    if operator == BETWEEN_OPERATOR:
        if rule.value is None or rule.value_b is None:
            return (
                STATUS_UNKNOWN,
                Severity.WARNING,
                _("Rule '%(rule)s' missing bounds for 'between'.")
                % {"rule": rule.identifier},
            )
        lower = _coerce_number(rule.value)
        upper = _coerce_number(rule.value_b)
        actual_num = _coerce_number(actual)
        if lower is None or upper is None or actual_num is None:
            return (
                STATUS_UNKNOWN,
                Severity.WARNING,
                _("Cannot evaluate 'between' for rule '%(rule)s'.")
                % {"rule": rule.identifier},
            )
        ok = lower <= actual_num <= upper
        if ok:
            return (
                STATUS_PASS,
                Severity.INFO,
                _("Value within expected range for rule '%(rule)s'.")
                % {"rule": rule.identifier},
            )
        return STATUS_FAIL, Severity.ERROR, rule.message
    if operator in IN_OPERATORS:
        options: set[str] = set()
        value = rule.value
        if isinstance(value, (list, tuple)):
            options = {str(v).strip() for v in value}
        elif isinstance(value, str):
            options = {
                part.strip()
                for part in value.split(VALUE_OPTIONS_DELIMITER)
                if part.strip()
            }
        target = str(actual).strip()
        if operator == IN_OPERATOR:
            ok = target in options
            if ok:
                return (
                    STATUS_PASS,
                    Severity.INFO,
                    _("Value allowed for rule '%(rule)s'.") % {"rule": rule.identifier},
                )
            return STATUS_FAIL, Severity.ERROR, rule.message
        ok = target not in options
        if ok:
            return (
                STATUS_PASS,
                Severity.INFO,
                _("Value allowed for rule '%(rule)s'.") % {"rule": rule.identifier},
            )
        return STATUS_FAIL, Severity.ERROR, rule.message
    if operator == NONEMPTY_OPERATOR:
        if actual in EMPTY_VALUES:
            return STATUS_FAIL, Severity.ERROR, rule.message
        return (
            STATUS_PASS,
            Severity.INFO,
            _("Value present for rule '%(rule)s'.") % {"rule": rule.identifier},
        )
    return (
        STATUS_UNKNOWN,
        Severity.WARNING,
        _("Unsupported operator '%(operator)s' for rule '%(rule)s'.")
        % {"operator": operator, "rule": rule.identifier},
    )


def _build_issue(
    rule: PolicyRule,
    status: str,
    severity: Severity,
    path: str,
    detail: str,
) -> ValidationIssue | None:
    if status == STATUS_PASS:
        return None
    message = rule.message or detail
    return ValidationIssue(
        path=path or rule.path,
        message=f"{message} ({status})",
        severity=severity,
    )


def _heuristic_critiques(data: Any) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    def walk(node: Any, pointer: str = JSON_POINTER_ROOT) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if pointer == JSON_POINTER_ROOT:
                    next_pointer = f"{JSON_POINTER_ROOT}{JSON_POINTER_SEPARATOR}{key}"
                else:
                    next_pointer = f"{pointer}{JSON_POINTER_SEPARATOR}{key}"
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
                    stdev = (
                        pstdev(numeric_values)
                        if len(numeric_values) >= PSTDEV_MIN_SAMPLE_SIZE
                        else ZERO_STD_DEV_THRESHOLD
                    )
                    if (
                        len(numeric_values) >= SCHEDULE_CONTINUOUS_MIN_LENGTH
                        and avg > SCHEDULE_AVERAGE_THRESHOLD
                    ):
                        issues.append(
                            ValidationIssue(
                                path=pointer,
                                message=_(
                                    "Schedule appears to run 24/7 (average %.2f).",
                                )
                                % avg,
                                severity=Severity.WARNING,
                            ),
                        )
                    if (
                        math.isclose(stdev, ZERO_STD_DEV_THRESHOLD)
                        and len(set(numeric_values)) == SINGLE_VALUE_VARIATION_COUNT
                        and len(numeric_values) >= FLAT_SERIES_MIN_LENGTH
                    ):
                        issues.append(
                            ValidationIssue(
                                path=pointer,
                                message=_(
                                    "Numeric series is flat; check "
                                    "if variation was expected.",
                                ),
                                severity=Severity.INFO,
                            ),
                        )
                    for idx, value in enumerate(numeric_values):
                        if value < NEGATIVE_THRESHOLD:
                            index_pointer = (
                                f"{pointer}{LIST_INDEX_TEMPLATE.format(index=idx)}"
                            )
                            issues.append(
                                ValidationIssue(
                                    path=index_pointer,
                                    message=_(
                                        "Negative value %(value)s : "
                                        "detected in schedule.",
                                    )
                                    % {"value": value},
                                    severity=Severity.WARNING,
                                ),
                            )
            seen = set()
            for idx, value in enumerate(node):
                next_pointer = f"{pointer}{LIST_INDEX_TEMPLATE.format(index=idx)}"
                if isinstance(value, (int, float, str)):
                    if value in seen:
                        issues.append(
                            ValidationIssue(
                                path=next_pointer,
                                message=_(
                                    "Duplicate value '%(value)s' detected in list.",
                                )
                                % {
                                    "value": value,
                                },
                                severity=Severity.INFO,
                            ),
                        )
                    else:
                        seen.add(value)
                walk(value, next_pointer)
        else:
            if isinstance(node, (int, float)) and not isinstance(node, bool):
                if node < NEGATIVE_THRESHOLD:
                    issues.append(
                        ValidationIssue(
                            path=pointer,
                            message=_("Negative numeric value %(value)s detected.")
                            % {"value": node},
                            severity=Severity.WARNING,
                        ),
                    )
                key_lower = pointer.lower()
                if any(token in key_lower for token in TEMPERATURE_KEY_TOKENS):
                    if node < TEMPERATURE_MIN_C or node > TEMPERATURE_MAX_C:
                        issues.append(
                            ValidationIssue(
                                path=pointer,
                                message=_(
                                    "Temperature %(value)s°C appears outside a "
                                    "realistic comfort band.",
                                )
                                % {"value": node},
                                severity=Severity.WARNING,
                            ),
                        )
                if any(token in key_lower for token in POWER_KEY_TOKENS):
                    if abs(node) > POWER_ABSOLUTE_THRESHOLD:
                        issues.append(
                            ValidationIssue(
                                path=pointer,
                                message=_(
                                    "Value %(value)s looks unusually "
                                    "large; verify units."
                                )
                                % {"value": node},
                                severity=Severity.WARNING,
                            ),
                        )
            if isinstance(node, str):
                if len(node) > UPPERCASE_STRING_THRESHOLD and node == node.upper():
                    issues.append(
                        ValidationIssue(
                            path=pointer,
                            message=_(
                                "Long uppercase string detected; "
                                "confirm this is intentional.",
                            ),
                            severity=Severity.INFO,
                        ),
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
                    path=JSON_POINTER_ROOT,
                    message=_(
                        "AI assist currently supports JSON submissions. "
                        "Unable to parse this document.",
                    ),
                    severity=Severity.WARNING,
                ),
            )
            return ValidationResult(
                passed=False,
                issues=issues,
                stats={STAT_KEY_TEMPLATE: UNPARSED_TEMPLATE_NAME},
            )

        config = self.config or {}
        template = config.get(CONFIG_KEY_TEMPLATE, DEFAULT_TEMPLATE)
        selectors = config.get(CONFIG_KEY_SELECTORS, DEFAULT_SELECTORS)
        rules_raw = config.get(CONFIG_KEY_POLICY_RULES, DEFAULT_POLICY_RULES)
        mode = config.get(CONFIG_KEY_MODE, DEFAULT_MODE).upper()
        cost_cap = config.get(CONFIG_KEY_COST_CAP, DEFAULT_COST_CAP_CENTS)

        stats.update(
            {
                STAT_KEY_TEMPLATE: template,
                STAT_KEY_MODE: mode,
                STAT_KEY_COST_CAP: cost_cap,
                STAT_KEY_SELECTOR_COUNT: len(selectors or []),
                STAT_KEY_POLICY_RULE_COUNT: len(rules_raw or []),
            },
        )

        if selectors:
            snippets: dict[str, list[Any]] = {}
            for selector in selectors:
                matches = _resolve_path(parsed, selector)
                snippets[selector] = [value for _path, value in matches][
                    :SELECTOR_SAMPLE_LIMIT
                ]
            stats[STAT_KEY_SELECTOR_SAMPLES] = snippets

        issues.extend(_heuristic_critiques(parsed))

        if ruleset is not None:
            issues.extend(
                self.run_cel_assertions_for_stages(
                    ruleset=ruleset,
                    validator=validator,
                    input_payload=parsed,
                ),
            )

        if rules_raw:
            for rule_data in rules_raw:
                rule = PolicyRule(
                    identifier=rule_data.get(RULE_DATA_KEY_ID, DEFAULT_RULE_IDENTIFIER),
                    path=rule_data.get(RULE_DATA_KEY_PATH, DEFAULT_RULE_PATH),
                    operator=rule_data.get(
                        RULE_DATA_KEY_OPERATOR,
                        DEFAULT_RULE_OPERATOR,
                    ),
                    value=rule_data.get(RULE_DATA_KEY_VALUE),
                    value_b=rule_data.get(RULE_DATA_KEY_VALUE_B),
                    message=rule_data.get(RULE_DATA_KEY_MESSAGE)
                    or DEFAULT_RULE_MESSAGE,
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
                    if mode == MODE_BLOCKING and severity == Severity.ERROR:
                        severity = Severity.ERROR
                    issue = _build_issue(rule, status, severity, match_path, detail)
                    if issue:
                        if mode == MODE_BLOCKING and issue.severity != Severity.INFO:
                            issue.severity = Severity.ERROR
                        issues.append(issue)

        has_error = any(issue.severity == Severity.ERROR for issue in issues)
        result = ValidationResult(passed=not has_error, issues=issues, stats=stats)
        return result
