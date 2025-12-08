"""
Management command to clean up old callback receipts.

Callback receipts are used for idempotency - preventing duplicate processing
when Cloud Run retries callback deliveries. Old receipts are no longer needed
and can be safely deleted after the retention period.

Usage:
    python manage.py cleanup_callback_receipts
    python manage.py cleanup_callback_receipts --days 60
    python manage.py cleanup_callback_receipts --dry-run
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from validibot.validations.models import CallbackReceipt


class Command(BaseCommand):
    help = "Delete callback receipts older than retention period (default: 30 days)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Delete receipts older than this many days (default: 30)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without actually deleting",
        )

    def handle(self, *args, **options):
        days = options["days"]
        dry_run = options["dry_run"]

        cutoff = timezone.now() - timedelta(days=days)

        old_receipts = CallbackReceipt.objects.filter(received_at__lt=cutoff)
        count = old_receipts.count()

        if count == 0:
            msg = f"No callback receipts older than {days} days found."
            self.stdout.write(self.style.SUCCESS(msg))
            return

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"[DRY RUN] Would delete {count} callback receipt(s) "
                    f"older than {days} days (before {cutoff.isoformat()})."
                )
            )
            return

        deleted, _ = old_receipts.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {deleted} callback receipt(s) older than {days} days."
            )
        )
