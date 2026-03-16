"""
Unit tests for the findings persistence module.

This module tests the pure-function helpers in ``findings_persistence.py``
that normalize raw validator output into canonical ``ValidationIssue``
objects and coerce severity values.  These helpers run outside the database,
so the tests are fast unit tests (no ``TestCase`` / ``TransactionTestCase``).

Coverage focus:
- ``normalize_issue`` — dict, dataclass, and fallback branches, including
  the 5 000-character length cap on the fallback ``str()`` conversion.
- ``coerce_severity`` — valid enum, valid string, and unknown input.
- ``severity_value`` — enum, raw string, and fallback.
"""

import pytest

from validibot.validations.constants import Severity
from validibot.validations.services.findings_persistence import coerce_severity
from validibot.validations.services.findings_persistence import normalize_issue
from validibot.validations.services.findings_persistence import severity_value
from validibot.validations.validators.base import ValidationIssue

# ── normalize_issue ─────────────────────────────────────────────────────
# Validators may return issues as dicts, ValidationIssue dataclasses, or
# arbitrary objects.  normalize_issue must handle all three and always
# return a ValidationIssue.


class TestNormalizeIssueFromDataclass:
    """Pass-through when the input is already a ValidationIssue."""

    def test_returns_same_instance(self):
        """A ValidationIssue input should be returned unchanged — no copy."""
        issue = ValidationIssue(
            path="/a",
            message="ok",
            severity=Severity.WARNING,
        )
        assert normalize_issue(issue) is issue


class TestNormalizeIssueFromDict:
    """Dict-to-ValidationIssue conversion (the most common branch)."""

    def test_maps_all_fields(self):
        """Every recognized key in the dict should be carried over."""
        expected_assertion_id = 42
        raw = {
            "path": "/building",
            "message": "Missing insulation",
            "severity": "WARNING",
            "code": "E001",
            "meta": {"source": "epjson"},
            "assertion_id": expected_assertion_id,
        }
        result = normalize_issue(raw)
        assert result.path == "/building"
        assert result.message == "Missing insulation"
        assert result.severity == Severity.WARNING
        assert result.code == "E001"
        assert result.meta == {"source": "epjson"}
        assert result.assertion_id == expected_assertion_id

    def test_missing_keys_default_to_empty(self):
        """An empty dict should produce safe defaults, not raise."""
        result = normalize_issue({})
        assert result.path == ""
        assert result.message == ""
        assert result.severity == Severity.ERROR  # coerce_severity(None)
        assert result.code == ""

    def test_none_values_coerced_to_empty_strings(self):
        """Explicit None values should not leak through as 'None' strings."""
        result = normalize_issue({"path": None, "message": None, "code": None})
        assert result.path == ""
        assert result.message == ""
        assert result.code == ""


class TestNormalizeIssueFallback:
    """Fallback branch for arbitrary (non-dict, non-dataclass) inputs."""

    def test_string_input(self):
        """A plain string should become the message with ERROR severity."""
        result = normalize_issue("something went wrong")
        assert result.message == "something went wrong"
        assert result.severity == Severity.ERROR
        assert result.path == ""

    def test_integer_input(self):
        """Numeric inputs should be str()-converted without crashing."""
        result = normalize_issue(42)
        assert result.message == "42"

    def test_oversized_input_is_capped(self):
        """
        A malicious or buggy validator could return an object whose
        ``__str__`` produces megabytes of text.  The fallback caps the
        message at 5 000 characters to prevent unbounded memory usage.
        """
        max_length = 5_000
        huge = "x" * (max_length * 2)
        result = normalize_issue(huge)
        assert len(result.message) == max_length
        assert result.message == "x" * max_length

    def test_cap_preserves_short_strings(self):
        """Strings shorter than the cap should not be truncated."""
        result = normalize_issue("short")
        assert result.message == "short"


# ── coerce_severity ─────────────────────────────────────────────────────
# Validators may report severity as an enum member, a raw string, or
# something unexpected.  coerce_severity normalizes all of these.


class TestCoerceSeverity:
    """Convert arbitrary severity inputs to the Severity enum."""

    def test_enum_passthrough(self):
        """An enum member should be returned unchanged."""
        assert coerce_severity(Severity.WARNING) is Severity.WARNING

    def test_valid_string(self):
        """A valid severity string should be coerced to the enum."""
        assert coerce_severity("WARNING") == Severity.WARNING

    def test_invalid_string_defaults_to_error(self):
        """Unrecognized strings should default to ERROR (fail-safe)."""
        assert coerce_severity("CRITICAL") == Severity.ERROR

    def test_none_defaults_to_error(self):
        """None severity (missing key in dict) should default to ERROR."""
        assert coerce_severity(None) == Severity.ERROR


# ── severity_value ──────────────────────────────────────────────────────
# Converts a severity to the string stored on ValidationFinding.


class TestSeverityValue:
    """Convert severity inputs to the string stored in the database."""

    def test_enum_to_string(self):
        """Enum members should produce their .value string."""
        assert severity_value(Severity.WARNING) == "WARNING"

    def test_valid_string_passthrough(self):
        """A raw string that matches a Severity value should pass through."""
        assert severity_value("INFO") == "INFO"

    def test_invalid_string_defaults_to_error(self):
        """Unrecognized strings should default to ERROR."""
        assert severity_value("CRITICAL") == "ERROR"

    @pytest.mark.parametrize("value", [None, 42, []])
    def test_non_string_defaults_to_error(self, value):
        """Non-string, non-enum inputs should default to ERROR."""
        assert severity_value(value) == "ERROR"
