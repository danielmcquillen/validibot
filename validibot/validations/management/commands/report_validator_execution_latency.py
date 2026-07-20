"""Report provider-stage latency from durable execution-attempt timestamps.

The report is intentionally derived from persisted attempt and run records so
it can be retained as rollout evidence. It never reads provider request bodies,
credentials, envelopes, or logs. Groups are split by validator, deployment
kind, and immutable deployment revision so Job and Service samples cannot be
accidentally blended.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime
from datetime import timedelta
from typing import TypedDict
from typing import cast

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.utils import timezone

from validibot.validations.models import ExecutionAttempt

STAGES = {
    "worker_handoff": ("dispatch_started_at", "provider_accepted_at"),
    "provider_start": ("provider_accepted_at", "provider_started_at"),
    "domain_execution": ("provider_started_at", "provider_finished_at"),
    "callback_finalize": ("provider_finished_at", "callback_received_at"),
    "provider_total": ("provider_accepted_at", "callback_received_at"),
    "run_total": (
        "step_run.validation_run.created",
        "step_run.validation_run.ended_at",
    ),
}


class _StageSummary(TypedDict):
    """Percentile summary for one provider lifecycle stage."""

    count: int
    p50_seconds: float | None
    p95_seconds: float | None


class _GroupAccumulator(TypedDict):
    """Mutable samples accumulated for one immutable deployment route."""

    attempt_count: int
    terminal_count: int
    stages: dict[str, list[float]]


class _ReportRow(TypedDict):
    """Stable output row used by both JSON and human renderers."""

    validator: str
    deployment_kind: str
    deployment_revision: str
    attempt_count: int
    terminal_count: int
    acceptance_ready: bool
    stages: dict[str, _StageSummary]


def _read_attribute(value: object, dotted_name: str) -> datetime | None:
    """Read one known dotted attribute path without evaluating user input."""
    for part in dotted_name.split("."):
        value = getattr(value, part, None)
        if value is None:
            break
    return cast("datetime | None", value)


def _percentile(values: list[float], percentile: float) -> float | None:
    """Return the deterministic nearest-rank percentile in seconds."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return round(ordered[rank - 1], 3)


def _stage_summary(values: list[float]) -> _StageSummary:
    """Summarize one timing stage without manufacturing missing samples."""
    return {
        "count": len(values),
        "p50_seconds": _percentile(values, 0.50),
        "p95_seconds": _percentile(values, 0.95),
    }


def _deployment_identity(attempt: ExecutionAttempt) -> tuple[str, str, str]:
    """Return validator, kind, and revision from the immutable route snapshot."""
    snapshot = attempt.deployment_snapshot
    if not isinstance(snapshot, dict):
        snapshot = {}
    validator = "legacy"
    deployment_kind = str(snapshot.get("deployment_kind") or "legacy")
    revision = str(snapshot.get("deployment_revision") or "legacy")
    if attempt.deployment_id and attempt.deployment is not None:
        validator = attempt.deployment.validator.slug
        deployment_kind = attempt.deployment.deployment_kind
        revision = attempt.deployment.deployment_revision
    elif attempt.step_run.workflow_step.validator is not None:
        validator = attempt.step_run.workflow_step.validator.slug
    return validator, deployment_kind, revision


class Command(BaseCommand):
    """Produce a stable human or JSON latency report for rollout decisions."""

    help = "Report p50/p95 validator execution latency by deployment revision."

    def add_arguments(self, parser):
        """Register bounded report-window and output-format options."""
        parser.add_argument(
            "--since-hours",
            type=int,
            default=168,
            help="Include attempts created in the last N hours (default: 168).",
        )
        parser.add_argument(
            "--minimum-samples",
            type=int,
            default=20,
            help="Sample count required for an acceptance-ready group (default: 20).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit stable JSON for retention or dashboard ingestion.",
        )

    def handle(self, *args, **options):
        """Aggregate non-negative timestamp pairs without inferring missing data."""
        since_hours = options["since_hours"]
        minimum_samples = options["minimum_samples"]
        if since_hours < 1:
            raise CommandError("--since-hours must be greater than zero")
        if minimum_samples < 1:
            raise CommandError("--minimum-samples must be greater than zero")

        generated_at = timezone.now()
        since = generated_at - timedelta(hours=since_hours)
        grouped: dict[tuple[str, str, str], _GroupAccumulator] = defaultdict(
            lambda: {
                "attempt_count": 0,
                "terminal_count": 0,
                "stages": {stage: [] for stage in STAGES},
            }
        )
        attempts = (
            ExecutionAttempt.objects.filter(created__gte=since)
            .select_related(
                "deployment__validator",
                "step_run__validation_run",
                "step_run__workflow_step__validator",
            )
            .order_by("created", "pk")
        )
        for attempt in attempts:
            group = grouped[_deployment_identity(attempt)]
            group["attempt_count"] += 1
            if attempt.is_terminal:
                group["terminal_count"] += 1
            stage_values = group["stages"]
            for stage_name, (start_name, end_name) in STAGES.items():
                start = _read_attribute(attempt, start_name)
                end = _read_attribute(attempt, end_name)
                if start is None or end is None or end < start:
                    continue
                stage_values[stage_name].append((end - start).total_seconds())

        rows: list[_ReportRow] = []
        for (validator, kind, revision), values in sorted(grouped.items()):
            stage_summaries = {
                stage_name: _stage_summary(stage_values)
                for stage_name, stage_values in values["stages"].items()
            }
            acceptance_samples = stage_summaries["provider_start"]["count"]
            rows.append(
                {
                    "validator": validator,
                    "deployment_kind": kind,
                    "deployment_revision": revision,
                    "attempt_count": values["attempt_count"],
                    "terminal_count": values["terminal_count"],
                    "acceptance_ready": acceptance_samples >= minimum_samples,
                    "stages": stage_summaries,
                }
            )

        report = {
            "generated_at": generated_at.isoformat(),
            "since": since.isoformat(),
            "minimum_samples": minimum_samples,
            "groups": rows,
        }
        if options["json"]:
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
            return
        if not rows:
            self.stdout.write("No validator execution attempts in the report window.")
            return
        for row in rows:
            provider_start = row["stages"]["provider_start"]
            readiness = "ready" if row["acceptance_ready"] else "needs samples"
            self.stdout.write(
                f"{row['validator']} {row['deployment_kind']} "
                f"{row['deployment_revision']}: attempts={row['attempt_count']}, "
                f"provider-start p50={provider_start['p50_seconds']}s "
                f"p95={provider_start['p95_seconds']}s ({readiness})"
            )
