from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from simplevalidations.projects.models import Project


class Command(BaseCommand):
    help = "Permanently delete soft-deleted projects older than N days."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            required=True,
            help="Soft-deleted projects older than this many days will be removed.",
        )

    def handle(self, *args, **options):
        days = options["days"]
        if days < 0:
            raise CommandError("Days must be zero or a positive integer.")

        cutoff = timezone.now() - timedelta(days=days)
        queryset = Project.all_objects.filter(
            is_active=False,
            deleted_at__lte=cutoff,
            is_default=False,
        )
        count = queryset.count()
        project_ids = list(queryset.values_list("id", flat=True))
        queryset.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {count} project(s): {', '.join(str(pid) for pid in project_ids)}",
            ),
        )
