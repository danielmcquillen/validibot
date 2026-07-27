"""
Management command to process failed purge retries.

This command processes PurgeRetry records that are due for retry
(next_retry_at <= now) and attempts to purge the associated submissions.

This command should be scheduled to run periodically (e.g., every 5 minutes
via Cloud Scheduler or cron).

Usage:
    python manage.py process_purge_retries
    python manage.py process_purge_retries --batch-size 50
    python manage.py process_purge_retries --dry-run
"""

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Exists
from django.db.models import OuterRef
from django.db.models import Q
from django.utils import timezone

from validibot.submissions.constants import SubmissionRetention
from validibot.submissions.models import PurgeRetry
from validibot.submissions.models import Submission
from validibot.submissions.models import SubmissionPurgeNotReadyError
from validibot.submissions.models import queue_submission_purge
from validibot.validations.constants import VALIDATION_RUN_TERMINAL_STATUSES
from validibot.validations.models import ValidationRun

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Process pending purge retries for submissions that failed to purge."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=50,
            help="Number of retries to process per run (default: 50)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be processed without actually processing",
        )

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        dry_run = options["dry_run"]

        now = timezone.now()
        self._discover_missing_do_not_store_work(now=now, batch_size=batch_size)
        # Discovery timestamps new rows with its own current time, which can be
        # a few milliseconds later than the snapshot above. Refresh before the
        # due-work query so repaired work is processed in this same sweep.
        now = timezone.now()

        # Privacy-critical deletion is never abandoned. Attempts beyond the
        # alert threshold continue at the capped backoff interval.
        pending_retries = (
            PurgeRetry.objects.filter(
                next_retry_at__lte=now,
            )
            .select_related("submission")
            .order_by("next_retry_at")[:batch_size]
        )

        retries_to_process = list(pending_retries)

        if not retries_to_process:
            self.stdout.write(
                self.style.SUCCESS("No pending purge retries to process."),
            )
            return

        self.stdout.write(f"Processing {len(retries_to_process)} purge retry(ies)")

        success_count = 0
        fail_count = 0
        skip_count = 0

        for retry in retries_to_process:
            submission = retry.submission

            # Check if already purged (by another process)
            if submission.content_purged_at:
                if not dry_run:
                    retry.delete()
                self.stdout.write(f"  Skipped (already purged): {submission.id}")
                skip_count += 1
                continue

            if dry_run:
                self.stdout.write(
                    f"  [DRY RUN] Would retry purge: {submission.id} "
                    f"(attempt {retry.attempt_count + 1})"
                )
                success_count += 1
                continue

            try:
                with transaction.atomic():
                    submission.purge_content()
                    # Delete the retry record on success
                    retry.delete()

                self.stdout.write(
                    self.style.SUCCESS(
                        f"  Purged: {submission.id} (attempt {retry.attempt_count + 1})"
                    )
                )
                success_count += 1

            except SubmissionPurgeNotReadyError:
                # A sibling run can become active between queueing and this
                # worker acquiring the submission. Deferral is normal lifecycle
                # state, not a failed deletion attempt.
                retry.next_retry_at = timezone.now() + timedelta(minutes=5)
                retry.save(update_fields=["next_retry_at"])
                skip_count += 1
                self.stdout.write(
                    f"  Deferred (active run): {submission.id}",
                )
            except Exception as e:
                fail_count += 1
                retry.record_failure(str(e))
                self.stdout.write(
                    self.style.ERROR(
                        f"  Failed: {submission.id} "
                        f"(attempt {retry.attempt_count}, "
                        f"next retry: {retry.next_retry_at})"
                    )
                )
                logger.exception(
                    "Purge retry failed",
                    extra={
                        "submission_id": str(submission.id),
                        "attempt_count": retry.attempt_count,
                    },
                )

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"[DRY RUN] Would have processed {success_count} retry(ies)."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Complete. Success: {success_count}, "
                    f"Failed: {fail_count}, Skipped: {skip_count}"
                )
            )

        # Report on retries that crossed the operator alert threshold while
        # continuing to retry them automatically.
        stale_count = PurgeRetry.objects.filter(
            attempt_count__gte=PurgeRetry.MAX_ATTEMPTS,
        ).count()
        if stale_count > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"Warning: {stale_count} retry(ies) crossed the alert threshold; "
                    "automatic retries will continue and operators should investigate."
                )
            )

    @staticmethod
    def _discover_missing_do_not_store_work(*, now, batch_size: int) -> None:
        """Repair missed terminal hooks by recreating absent purge work.

        A no-run submission is eligible only after a one-hour admission grace,
        preventing a scheduler race between submission creation and run
        creation while still cleaning up abandoned launches.
        """

        related_runs = ValidationRun.objects.filter(submission_id=OuterRef("pk"))
        active_runs = related_runs.exclude(
            status__in=VALIDATION_RUN_TERMINAL_STATUSES,
        )
        candidates = (
            Submission.objects.filter(
                retention_policy=SubmissionRetention.DO_NOT_STORE,
                content_purged_at__isnull=True,
                purge_retries__isnull=True,
            )
            .annotate(
                has_runs=Exists(related_runs),
                has_active_runs=Exists(active_runs),
            )
            .filter(has_active_runs=False)
            .filter(
                Q(has_runs=True) | Q(created__lte=now - timedelta(hours=1)),
            )
            .order_by("created")[:batch_size]
        )
        for submission in candidates:
            queue_submission_purge(submission)
