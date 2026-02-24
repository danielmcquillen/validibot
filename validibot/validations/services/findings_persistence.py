"""
Findings persistence — normalizing validation issues and persisting them as findings.

This module handles the conversion of raw validation issues (from validators, handlers,
and processors) into normalized ValidationFinding database records. It provides:

- Issue normalization: Converts dicts, strings, or ValidationIssue objects into a
  consistent ValidationIssue dataclass format.
- Severity coercion: Maps arbitrary severity inputs to the Severity enum.
- Bulk persistence: Creates ValidationFinding rows efficiently via bulk_create.
- Assertion failure counting: Tracks ERROR-severity assertion failures separately
  from other findings (WARNING/INFO assertions are informational, not blocking).

This was extracted from ValidationRunService to follow single-responsibility:
the orchestrator decides *when* to persist findings, this module handles *how*.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING
from typing import Any

from validibot.validations.constants import Severity
from validibot.validations.models import ValidationFinding
from validibot.validations.validators.base import ValidationIssue

if TYPE_CHECKING:
    from validibot.validations.models import ValidationRun
    from validibot.validations.models import ValidationStepRun

logger = logging.getLogger(__name__)


def normalize_issue(issue: Any) -> ValidationIssue:
    """Ensure every issue is a ValidationIssue dataclass."""
    if isinstance(issue, ValidationIssue):
        return issue
    if isinstance(issue, dict):
        severity = coerce_severity(issue.get("severity"))
        return ValidationIssue(
            path=str(issue.get("path", "") or ""),
            message=str(issue.get("message", "") or ""),
            severity=severity,
            code=str(issue.get("code", "") or ""),
            meta=issue.get("meta"),
            assertion_id=issue.get("assertion_id"),
        )
    return ValidationIssue(
        path="",
        message=str(issue),
        severity=Severity.ERROR,
    )


def coerce_severity(value: Any) -> Severity:
    """Convert arbitrary severity input to a Severity choice."""
    if isinstance(value, Severity):
        return value
    if isinstance(value, str):
        try:
            return Severity(value)
        except ValueError:
            pass
    return Severity.ERROR


def severity_value(value: Severity | str | None) -> str:
    """Return the string value that should be stored on ValidationFinding."""
    if isinstance(value, Severity):
        return value.value
    if isinstance(value, str) and value in Severity.values:
        return value
    return Severity.ERROR


def persist_findings(
    *,
    validation_run: ValidationRun,
    step_run: ValidationStepRun,
    issues: list[ValidationIssue],
) -> tuple[Counter, int]:
    """
    Persist ValidationFinding rows and return severity counts.

    Creates findings in bulk for efficiency. Each issue is converted to a
    ValidationFinding with proper severity, code, message, path, and metadata.

    Assertion failures are counted separately: only ERROR-severity assertion
    issues count as failures. WARNING/INFO assertions that evaluate to false
    are tracked as issues but don't count toward the failure total — they're
    intentionally configured as non-blocking by the ruleset author.

    Args:
        validation_run: The parent run (for denormalized FK).
        step_run: The step run these findings belong to.
        issues: Normalized ValidationIssue objects to persist.

    Returns:
        Tuple of (severity_counts Counter, assertion_failure_count int).
    """
    severity_counts: Counter = Counter()
    assertion_failures = 0
    findings: list[ValidationFinding] = []
    for issue in issues:
        sev_value = severity_value(issue.severity)
        severity_counts[sev_value] += 1
        # Count assertion failures: only ERROR-severity assertion issues.
        # WARNING/INFO assertions that evaluate to false are tracked as issues
        # but don't count toward the failure total - they're intentionally
        # configured as non-blocking by the author.
        if issue.assertion_id and sev_value == Severity.ERROR:
            assertion_failures += 1
        meta = issue.meta or {}
        if meta and not isinstance(meta, dict):
            meta = {"detail": meta}
        finding = ValidationFinding(
            validation_run=validation_run,
            validation_step_run=step_run,
            severity=sev_value,
            code=issue.code or "",
            message=issue.message or "",
            path=issue.path or "",
            meta=meta,
            ruleset_assertion_id=issue.assertion_id,
        )
        finding._ensure_run_alignment()
        finding._strip_payload_prefix()
        findings.append(finding)
    if findings:
        ValidationFinding.objects.bulk_create(findings, batch_size=500)
    return severity_counts, assertion_failures
