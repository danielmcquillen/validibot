"""
Management command to clean up expired idempotency keys.

Idempotency keys are used to prevent duplicate API requests. Each key has an
expiration time (default 24 hours), after which it can be safely deleted.

Usage:
    python manage.py cleanup_idempotency_keys
    python manage.py cleanup_idempotency_keys --dry-run
"""

from django.core.management.base import BaseCommand
from django.utils import timezone

from validibot.core.models import IdempotencyKey


class Command(BaseCommand):
    help = "Delete expired idempotency keys."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be deleted without actually deleting",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        now = timezone.now()

        expired_keys = IdempotencyKey.objects.filter(expires_at__lt=now)
        count = expired_keys.count()

        if count == 0:
            self.stdout.write(self.style.SUCCESS("No expired idempotency keys found."))
            return

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"[DRY RUN] Would delete {count} expired idempotency key(s)."
                )
            )
            return

        deleted, _ = expired_keys.delete()
        self.stdout.write(
            self.style.SUCCESS(f"Deleted {deleted} expired idempotency key(s).")
        )
