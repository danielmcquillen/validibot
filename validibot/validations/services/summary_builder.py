"""
Summary builder — aggregating run and step summaries from persisted findings.

This module handles building ValidationRunSummary and ValidationStepRunSummary
records from the database. It queries persisted findings rather than relying on
in-memory metrics, making it safe to call in resume scenarios (async callbacks,
retries) where earlier steps' findings are already persisted.

Responsibilities:

- Run-level severity aggregation: Counts ERROR/WARNING/INFO findings across all steps.
- Assertion accounting: Totals assertion failures and counts from step output fields.
- Step-level summaries: Per-step finding breakdowns for the summary detail view.
- Idempotent rebuild: Safe to call multiple times via rebuild_run_summary_record().

This was extracted from ValidationRunService to follow single-responsibility:
the orchestrator decides *when* to build summaries, this module handles *how*.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING
from typing import Any

from django.db.models import Count

from validibot.validations.constants import Severity
from validibot.validations.models import ValidationFinding
from validibot.validations.models import ValidationRunSummary
from validibot.validations.models import ValidationStepRun
from validibot.validations.models import ValidationStepRunSummary

if TYPE_CHECKING:
    from validibot.validations.models import ValidationRun

logger = logging.getLogger(__name__)


def extract_assertion_total(stats: dict[str, Any] | None) -> int:
    """Extract the assertion total count from a stats dict."""
    if not isinstance(stats, dict):
        return 0
    # Check all the possible keys where assertion total might be stored
    for key in ("assertion_total", "assertion_count", "assertions_evaluated"):
        value = stats.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def rebuild_run_summary_record(
    *,
    validation_run: ValidationRun,
) -> ValidationRunSummary:
    """
    Rebuild run and step summary records from persisted state.

    This is safe to call multiple times and is used when a run reaches a
    terminal state outside of the main worker loop (for example completion
    via an async validator callback).

    Args:
        validation_run: The run whose summary records should be rebuilt.

    Returns:
        The updated ValidationRunSummary record.
    """
    return build_run_summary_record(
        validation_run=validation_run,
        step_metrics=[],
    )


def build_run_summary_record(
    *,
    validation_run: ValidationRun,
    step_metrics: list[dict[str, Any]],
) -> ValidationRunSummary:
    """
    Build run and step summary records from database findings.

    This method queries persisted findings from the database rather than
    relying solely on in-memory step_metrics. This ensures accurate summaries
    in resume scenarios where earlier steps' findings are already persisted
    but not in the current step_metrics list.

    Assertion totals are computed from persisted step_run.output data. The
    step_metrics argument is accepted for call-site compatibility, but the
    summary is rebuilt from persisted state so it can be called safely after
    async callbacks and retries.
    """
    # Query run-level severity counts from persisted findings
    # This ensures we include findings from ALL steps, not just current pass
    severity_totals: Counter[str] = Counter()
    for row in (
        ValidationFinding.objects.filter(validation_run=validation_run)
        .values("severity")
        .annotate(count=Count("id"))
    ):
        severity_totals[row["severity"]] = row["count"]

    total_findings = sum(severity_totals.values())

    # Query assertion counts from ALL step runs' output fields.
    # This ensures correct totals in resume scenarios where earlier steps'
    # metrics aren't in the current step_metrics list.
    all_step_runs = (
        ValidationStepRun.objects.filter(
            validation_run=validation_run,
        )
        .select_related("workflow_step")
        .order_by("step_order")
    )

    assertion_failures = 0
    assertion_total = 0
    for step_run in all_step_runs:
        output = step_run.output or {}
        assertion_failures += output.get("assertion_failures", 0)
        # assertion_total comes from stats under various keys
        assertion_total += extract_assertion_total(output)

    summary_record, _ = ValidationRunSummary.objects.update_or_create(
        run=validation_run,
        defaults={
            "status": validation_run.status,
            "completed_at": validation_run.ended_at,
            "total_findings": total_findings,
            "error_count": severity_totals.get(Severity.ERROR, 0),
            "warning_count": severity_totals.get(Severity.WARNING, 0),
            "info_count": severity_totals.get(Severity.INFO, 0),
            "assertion_failure_count": assertion_failures,
            "assertion_total_count": assertion_total,
            "extras": {},
        },
    )

    # Build step summaries from ALL step runs, querying findings from DB
    # (reuses all_step_runs queryset from assertion counting above)
    summary_record.step_summaries.all().delete()
    step_summary_objects: list[ValidationStepRunSummary] = []

    for step_run in all_step_runs:
        # Query step-level severity counts from persisted findings
        step_severity_counts: Counter[str] = Counter()
        for row in (
            ValidationFinding.objects.filter(validation_step_run=step_run)
            .values("severity")
            .annotate(count=Count("id"))
        ):
            step_severity_counts[row["severity"]] = row["count"]

        step_summary_objects.append(
            ValidationStepRunSummary(
                summary=summary_record,
                step_run=step_run,
                step_name=getattr(
                    step_run.workflow_step,
                    "name",
                    "",
                ),
                step_order=step_run.step_order or 0,
                status=step_run.status,
                error_count=step_severity_counts.get(Severity.ERROR, 0),
                warning_count=step_severity_counts.get(Severity.WARNING, 0),
                info_count=step_severity_counts.get(Severity.INFO, 0),
            ),
        )

    if step_summary_objects:
        ValidationStepRunSummary.objects.bulk_create(step_summary_objects)

    return summary_record
