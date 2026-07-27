"""
Management command to purge expired validation run outputs.

This command processes validation runs that have passed their output retention
period (output_expires_at < now) and purges their outputs (findings, artifacts,
and storage files). The run record is preserved for audit trail.

This is separate from purge_expired_submissions, which handles user-submitted
files. Authors configure the two windows independently and both default to no
post-processing retention.

This command should be scheduled to run frequently (every five minutes via
Cloud Scheduler or cron).

Usage:
    python manage.py purge_expired_outputs
    python manage.py purge_expired_outputs --batch-size 100
    python manage.py purge_expired_outputs --dry-run

Environment:
    This command is designed to run on worker instances where it has access
    to storage for deleting run files and artifacts.
"""

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from validibot.submissions.constants import OutputRetention
from validibot.validations.constants import VALIDATION_RUN_TERMINAL_STATUSES
from validibot.validations.models import ValidationRun
from validibot.validations.services.retention import purge_run_outputs
from validibot.validations.services.retention import schedule_terminal_retention

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Purge outputs from validation runs that have passed their retention period."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Number of runs to process per batch (default: 100)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be purged without actually purging",
        )
        parser.add_argument(
            "--max-batches",
            type=int,
            default=10,
            help="Maximum number of batches to process (default: 10, 0=unlimited)",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        dry_run = options["dry_run"]
        max_batches = options["max_batches"]

        now = timezone.now()
        self._repair_missing_expiries()
        total_purged = 0
        total_failed = 0
        batch_count = 0

        while True:
            # Check batch limit
            if max_batches > 0 and batch_count >= max_batches:
                self.stdout.write(
                    self.style.WARNING(
                        f"Reached max batch limit ({max_batches}). "
                        f"Purged {total_purged}, failed {total_failed}."
                    )
                )
                break

            # Terminal status is a hard safety boundary: launch-time clocks or
            # corrupt rows must never let a sweeper delete a running bundle.
            # DO_NOT_STORE rows are independently discoverable even if their
            # terminal signal failed before writing output_expires_at.
            expired_runs = (
                ValidationRun.objects.filter(
                    status__in=VALIDATION_RUN_TERMINAL_STATUSES,
                    output_purged_at__isnull=True,
                )
                .filter(
                    (
                        Q(output_expires_at__lte=now)
                        & ~Q(
                            output_retention_policy=OutputRetention.DO_NOT_STORE,
                        )
                    )
                    | (
                        Q(output_retention_policy=OutputRetention.DO_NOT_STORE)
                        & Q(ended_at__lte=now - timedelta(minutes=1))
                    ),
                )
                .exclude(
                    output_retention_policy=OutputRetention.STORE_PERMANENTLY,
                )
                .order_by("output_expires_at")[:batch_size]
            )

            # Convert to list to avoid queryset changes during iteration
            runs_to_process = list(expired_runs)

            if not runs_to_process:
                if total_purged == 0 and total_failed == 0:
                    self.stdout.write(
                        self.style.SUCCESS("No expired outputs to purge.")
                    )
                else:
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"Completed. Purged {total_purged}, failed {total_failed}."
                        )
                    )
                break

            batch_count += 1
            self.stdout.write(
                f"Processing batch {batch_count}: {len(runs_to_process)} run(s)"
            )

            for run in runs_to_process:
                if dry_run:
                    self.stdout.write(
                        f"  [DRY RUN] Would purge outputs: {run.id} "
                        f"(policy={run.output_retention_policy}, "
                        f"expires={run.output_expires_at})"
                    )
                    total_purged += 1
                    continue

                try:
                    with transaction.atomic():
                        purge_run_outputs(run)
                    total_purged += 1
                    self.stdout.write(self.style.SUCCESS(f"  Purged outputs: {run.id}"))
                except Exception as e:
                    total_failed += 1
                    self.stdout.write(
                        self.style.ERROR(f"  Failed to purge outputs {run.id}: {e}")
                    )
                    logger.exception(
                        "Failed to purge expired run outputs",
                        extra={"run_id": str(run.id)},
                    )
                    # Continue with other runs - don't stop on failure

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"[DRY RUN] Would have purged outputs for {total_purged} run(s)."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Purge complete. Purged: {total_purged}, Failed: {total_failed}"
                )
            )

        # Return non-zero exit code if there were failures
        if total_failed > 0:
            self.stderr.write(
                self.style.ERROR(
                    f"{total_failed} run(s) failed to purge. Check logs and retry."
                )
            )

    @staticmethod
    def _repair_missing_expiries() -> None:
        """Rebuild terminal deadlines missed by a failed signal receiver."""

        missing = (
            ValidationRun.objects.filter(
                status__in=VALIDATION_RUN_TERMINAL_STATUSES,
                output_purged_at__isnull=True,
                output_expires_at__isnull=True,
            )
            .exclude(output_retention_policy=OutputRetention.STORE_PERMANENTLY)
            .select_related("submission")
            .iterator(chunk_size=200)
        )
        for run in missing:
            schedule_terminal_retention(run)
