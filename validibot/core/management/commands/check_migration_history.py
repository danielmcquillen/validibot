"""Refuse migration when the database records a removed pre-reset tail.

Run this immediately before ``manage.py migrate``. It is read-only: the
command compares rows in Django's migration recorder with the migration files
shipped by the current image. An incompatible result stops deployment before
any schema operation runs and explains why the database needs an explicit
backup/rebuild decision.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import DEFAULT_DB_ALIAS
from django.db import connections
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder

from validibot.core.migration_safety import incompatible_reset_migrations


class Command(BaseCommand):
    """Check one database's recorded history without modifying its schema."""

    help = "Refuse migration when a database predates the current-schema reset."

    def add_arguments(self, parser):
        """Accept Django's database alias for multi-database deployments."""
        parser.add_argument(
            "--database",
            default=DEFAULT_DB_ALIAS,
            choices=tuple(connections),
            help="Database alias to inspect (default: default).",
        )

    def handle(self, *args, **options):
        """Compare applied records with disk migrations and fail before writes."""
        database = str(options["database"])
        recorder = MigrationRecorder(connections[database])
        applied = recorder.applied_migrations()
        known = MigrationLoader(None, ignore_no_migrations=True).disk_migrations
        incompatible = incompatible_reset_migrations(
            applied_migrations=applied,
            known_migrations=known,
        )
        if incompatible:
            examples = ", ".join(incompatible[:5])
            remainder = len(incompatible) - 5
            suffix = f" (+{remainder} more)" if remainder > 0 else ""
            raise CommandError(
                "Database migration history predates Validibot's 2026-07-16 "
                f"current-schema reset ({len(incompatible)} incompatible "
                f"records): {examples}{suffix}. No schema changes were made. "
                "Do not run migrate against this database; take a backup and "
                "provision a fresh database through the documented deployment "
                "flow, or contact the maintainer before preserving legacy data."
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Migration history is compatible on database '{database}'."
            )
        )
