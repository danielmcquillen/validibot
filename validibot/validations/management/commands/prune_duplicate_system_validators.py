"""
Legacy cleanup command for duplicate system Validator rows.

Why this exists
---------------
The Validator table is keyed by ``UniqueConstraint(slug, version)``, not by
``slug`` alone. Multiple rows with the same slug and different integer
versions are now legitimate version history: the library shows only the latest
row by default and keeps older rows available under hidden ``/versions/<n>/``
URLs.

Self-hosted operators upgrading across one of those bumps end up with two
"SHACL Validator" cards (one from the old version, one from the new) and no
clean Django-migration way to merge them without risking PROTECT'd FKs from
``WorkflowStep``.

What it does
------------
Older versions are therefore not pruned. The command remains as a harmless
operator diagnostic for installations that still have it in a runbook: it
reports versioned families and exits without rewriting FKs or deleting rows.

Usage
-----
    # Report versioned system validator families.
    python manage.py prune_duplicate_system_validators

    # Backward-compatible no-op; still reports only.
    python manage.py prune_duplicate_system_validators --commit
"""

from collections import defaultdict

from django.core.management.base import BaseCommand

from validibot.validations.models import Validator


class Command(BaseCommand):
    help = (
        "Report system Validator families with multiple versions. "
        "Version history is valid and is no longer pruned."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--commit",
            action="store_true",
            default=False,
            help="Accepted for backward compatibility; no rows are deleted.",
        )

    def handle(self, *args, **options):
        commit = options["commit"]
        mode_label = "COMMIT" if commit else "DRY-RUN"
        self.stdout.write(self.style.WARNING(f"Mode: {mode_label}"))

        groups: dict[str, list[Validator]] = defaultdict(list)
        for v in Validator.objects.filter(is_system=True).order_by("slug", "version"):
            groups[v.slug].append(v)

        had_version_history = False
        for slug, rows in groups.items():
            if len(rows) <= 1:
                continue
            had_version_history = True

            self.stdout.write("")
            self.stdout.write(
                self.style.NOTICE(
                    f"slug={slug!r}: {len(rows)} versioned row(s) retained "
                    f"{[(r.pk, r.version) for r in rows]}",
                ),
            )

        if not had_version_history:
            self.stdout.write(
                self.style.SUCCESS("No multi-version system validator families found."),
            )
        else:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "No rows were changed. Older validator versions are valid "
                    "history and remain addressable by version URL.",
                ),
            )
