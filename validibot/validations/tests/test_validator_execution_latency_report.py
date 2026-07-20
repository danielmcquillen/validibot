"""Tests for the retained validator execution latency report.

The rollout ADR requires p50/p95 measurements split by backend deployment and
provider stage. These tests prove the command never blends Job and Service
revisions, does not invent samples from missing timestamps, and exposes a
machine-readable acceptance signal only after the requested sample count.
"""

import json
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.tests.factories import ExecutionAttemptFactory

pytestmark = pytest.mark.django_db
EXPECTED_DEPLOYMENT_GROUPS = 2


def _timed_attempt(*, kind: str, revision: str, provider_start_seconds: int):
    """Create one complete timing chain for a distinct immutable deployment."""
    origin = timezone.now() - timedelta(minutes=5)
    return ExecutionAttemptFactory(
        state=ExecutionAttemptState.COMPLETED,
        deployment_snapshot={
            "deployment_kind": kind,
            "deployment_revision": revision,
        },
        dispatch_started_at=origin,
        provider_accepted_at=origin + timedelta(seconds=1),
        provider_started_at=origin + timedelta(seconds=1 + provider_start_seconds),
        provider_finished_at=origin + timedelta(seconds=20),
        callback_received_at=origin + timedelta(seconds=22),
        terminal_at=origin + timedelta(seconds=22),
    )


def test_report_keeps_provider_shapes_and_revisions_separate():
    """Job latency must never make a new Service revision appear acceptable."""
    _timed_attempt(kind="cloud_run_job", revision="job-v1", provider_start_seconds=9)
    _timed_attempt(
        kind="cloud_run_service",
        revision="service-v2",
        provider_start_seconds=3,
    )
    output = StringIO()

    call_command(
        "report_validator_execution_latency",
        "--json",
        "--minimum-samples=1",
        stdout=output,
    )

    report = json.loads(output.getvalue())
    groups = {
        (row["deployment_kind"], row["deployment_revision"]): row
        for row in report["groups"]
    }
    assert len(groups) == EXPECTED_DEPLOYMENT_GROUPS
    assert groups[("cloud_run_job", "job-v1")]["stages"]["provider_start"] == {
        "count": 1,
        "p50_seconds": 9.0,
        "p95_seconds": 9.0,
    }
    assert groups[("cloud_run_service", "service-v2")]["acceptance_ready"] is True


def test_report_omits_missing_timestamp_pairs_from_percentiles():
    """Partial historical timing cannot be counted as a zero-second startup."""
    attempt = _timed_attempt(
        kind="cloud_run_service",
        revision="service-v2",
        provider_start_seconds=3,
    )
    attempt.provider_started_at = None
    attempt.save(update_fields=["provider_started_at", "modified"])
    output = StringIO()

    call_command(
        "report_validator_execution_latency",
        "--json",
        "--minimum-samples=1",
        stdout=output,
    )

    row = json.loads(output.getvalue())["groups"][0]
    assert row["attempt_count"] == 1
    assert row["acceptance_ready"] is False
    assert row["stages"]["provider_start"] == {
        "count": 0,
        "p50_seconds": None,
        "p95_seconds": None,
    }
