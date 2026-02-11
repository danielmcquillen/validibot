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

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from validibot.submissions.models import PurgeRetry

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

        # Find retries that are due and haven't exceeded max attempts
        pending_retries = (
            PurgeRetry.objects.filter(
                next_retry_at__lte=now,
                attempt_count__lt=PurgeRetry.MAX_ATTEMPTS,
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

        # Report on retries that have exceeded max attempts
        stale_count = PurgeRetry.objects.filter(
            attempt_count__gte=PurgeRetry.MAX_ATTEMPTS,
        ).count()
        if stale_count > 0:
            self.stdout.write(
                self.style.WARNING(
                    f"Warning: {stale_count} retry(ies) have exceeded max attempts "
                    "and require manual intervention."
                )
            )
