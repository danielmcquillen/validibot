"""
Management command to mark stuck validation runs as failed.

Validation runs can become "stuck" in RUNNING status if a validator container
crashes without sending a callback, or if the callback fails to reach the
worker service. This watchdog command finds runs that have been in RUNNING
status longer than a threshold and marks them as FAILED.

This is a safety net for edge cases - most runs complete normally via callbacks.
The threshold should be generous (30+ minutes) to avoid false positives for
legitimately long-running validations like EnergyPlus simulations.

Usage:
    python manage.py cleanup_stuck_runs
    python manage.py cleanup_stuck_runs --timeout-minutes 60
    python manage.py cleanup_stuck_runs --dry-run

Environment:
    This command should be scheduled to run periodically (e.g., every 10 minutes)
    via Cloud Scheduler on the worker service.

See also:
    - docs/dev_docs/google_cloud/scheduled-jobs.md for scheduling setup
    - validibot/validations/api/callbacks.py for the normal completion path
"""

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from validibot.validations.constants import ValidationRunErrorCategory
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import ValidationRun

logger = logging.getLogger(__name__)

# Default timeout: 30 minutes is generous for most validations.
# EnergyPlus runs can take 5-15 minutes; this gives plenty of buffer.
DEFAULT_TIMEOUT_MINUTES = 30

# Max IDs to display in output before truncating
MAX_DISPLAY_IDS = 10


class Command(BaseCommand):
    help = "Mark validation runs stuck in RUNNING status as FAILED."

    def add_arguments(self, parser):
        parser.add_argument(
            "--timeout-minutes",
            type=int,
            default=DEFAULT_TIMEOUT_MINUTES,
            help=(
                f"Consider runs stuck after this many minutes (default: "
                f"{DEFAULT_TIMEOUT_MINUTES})"
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report stuck runs without modifying them",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Maximum number of runs to process per invocation (default: 100)",
        )

    def handle(self, *args, **options):
        timeout_minutes = options["timeout_minutes"]
        dry_run = options["dry_run"]
        batch_size = options["batch_size"]

        timeout = timedelta(minutes=timeout_minutes)
        cutoff = timezone.now() - timeout

        # Find runs that have been RUNNING for too long
        # We check started_at, not created_at, to measure actual run time
        stuck_runs = ValidationRun.objects.filter(
            status=ValidationRunStatus.RUNNING,
            started_at__lt=cutoff,
        ).order_by("started_at")[:batch_size]

        count = stuck_runs.count()

        if count == 0:
            self.stdout.write(
                self.style.SUCCESS(
                    f"No runs stuck longer than {timeout_minutes} minutes."
                )
            )
            return

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"[DRY RUN] Would mark {count} run(s) as FAILED:"
                )
            )
            for run in stuck_runs:
                minutes_running = (timezone.now() - run.started_at).total_seconds() / 60
                self.stdout.write(
                    f"  - {run.id}: running for {minutes_running:.1f} minutes "
                    f"(workflow={run.workflow_id})"
                )
            return

        # Mark runs as failed in a transaction
        error_message = (
            f"Run timed out after {timeout_minutes} minutes - "
            "no callback received from validator. This may indicate the validator "
            "crashed or the callback failed to reach the server."
        )

        updated_ids = []
        for run in stuck_runs:
            with transaction.atomic():
                # Re-fetch with lock to avoid race conditions
                locked = ValidationRun.objects.select_for_update().get(pk=run.pk)

                # Double-check status hasn't changed since our query
                if locked.status != ValidationRunStatus.RUNNING:
                    continue

                locked.status = ValidationRunStatus.FAILED
                locked.error_category = ValidationRunErrorCategory.TIMEOUT
                locked.error = error_message
                locked.ended_at = timezone.now()

                if locked.started_at and locked.ended_at:
                    locked.duration_ms = int(
                        (locked.ended_at - locked.started_at).total_seconds() * 1000
                    )

                locked.save(
                    update_fields=[
                        "status",
                        "error_category",
                        "error",
                        "ended_at",
                        "duration_ms",
                    ]
                )
                updated_ids.append(str(locked.id))

                workflow_id = locked.workflow_id
                logger.warning(
                    "Marked stuck run as FAILED",
                    extra={
                        "run_id": str(locked.id),
                        "workflow_id": str(workflow_id) if workflow_id else None,
                        "timeout_minutes": timeout_minutes,
                    },
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Marked {len(updated_ids)} stuck run(s) as FAILED "
                f"(timeout: {timeout_minutes} minutes)."
            )
        )

        if updated_ids:
            self.stdout.write(f"  IDs: {', '.join(updated_ids[:MAX_DISPLAY_IDS])}")
            if len(updated_ids) > MAX_DISPLAY_IDS:
                extra = len(updated_ids) - MAX_DISPLAY_IDS
                self.stdout.write(f"  ... and {extra} more")
