"""
Management command to purge expired submission content.

This command processes submissions that have passed their retention period
(expires_at < now) and purges their content. The submission record is preserved
for audit trail; only the content (inline text or uploaded file) is removed.

This command should be scheduled to run periodically (e.g., hourly via
Cloud Scheduler or cron).

Usage:
    python manage.py purge_expired_submissions
    python manage.py purge_expired_submissions --batch-size 100
    python manage.py purge_expired_submissions --dry-run

Environment:
    This command is designed to run on worker instances where it has access
    to GCS for deleting execution bundles.
"""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from validibot.submissions.models import Submission

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Purge content from submissions that have passed their retention period."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Number of submissions to process per batch (default: 100)",
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

            # Find expired submissions that haven't been purged yet
            expired_submissions = Submission.objects.filter(
                expires_at__lte=now,
                content_purged_at__isnull=True,
            ).order_by("expires_at")[:batch_size]

            # Convert to list to avoid queryset changes during iteration
            submissions_to_process = list(expired_submissions)

            if not submissions_to_process:
                if total_purged == 0 and total_failed == 0:
                    self.stdout.write(
                        self.style.SUCCESS("No expired submissions to purge.")
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
                f"Processing batch {batch_count}: {len(submissions_to_process)} "
                f"submission(s)"
            )

            for submission in submissions_to_process:
                if dry_run:
                    self.stdout.write(
                        f"  [DRY RUN] Would purge: {submission.id} "
                        f"(policy={submission.retention_policy}, "
                        f"expires={submission.expires_at})"
                    )
                    total_purged += 1
                    continue

                try:
                    with transaction.atomic():
                        submission.purge_content()
                    total_purged += 1
                    self.stdout.write(
                        self.style.SUCCESS(f"  Purged: {submission.id}")
                    )
                except Exception as e:
                    total_failed += 1
                    self.stdout.write(
                        self.style.ERROR(f"  Failed to purge {submission.id}: {e}")
                    )
                    logger.exception(
                        "Failed to purge expired submission",
                        extra={"submission_id": str(submission.id)},
                    )
                    # Continue with other submissions - don't stop on failure

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"[DRY RUN] Would have purged {total_purged} submission(s)."
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
                    f"{total_failed} submission(s) failed to purge. "
                    "Check logs and retry."
                )
            )
