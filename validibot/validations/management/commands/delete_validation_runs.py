"""Management command to delete validation runs (and their cascade).

Use cases:

- Reset a demo or tutorial workflow so its SHACL/JSON ruleset re-unlocks
  for editing (rulesets are immutable once their workflow has any runs).
- Clean up stuck or experimental runs accumulated during development.
- Wipe a specific workflow's run history without touching unrelated data.

Why a command and not the shell:

- Cloud Run is stateless; there is no shell to attach to in production.
  ``just gcp management-cmd <stage> "delete_validation_runs ..."`` is
  the canonical way to run one-off ops work against deployed services.
- Credential rows (``IssuedCredential``,
  ``ValidationCredentialDigestMetadata``) use ``on_delete=PROTECT``, so
  a naive ``ValidationRun.objects.delete()`` raises ``ProtectedError``
  on workflows that ran the signed-credential action. This command
  handles that cascade explicitly so the operator does not have to.

Safety:

- ``--dry-run`` (default) reports counts without deleting anything.
- ``--confirm`` is required for any actual deletion. Running without it
  prints the plan and exits non-zero, so an accidental invocation in
  CI or a job harness cannot wipe data silently.
- The command refuses to delete across the whole database unless
  ``--all`` is passed *and* ``--confirm`` is passed. Scoping by
  ``--workflow-id`` or ``--org-id`` is preferred.

Examples::

    # See what would be deleted on workflow 4 (no changes made)
    python manage.py delete_validation_runs --workflow-id 4

    # Actually delete runs on workflow 4 (and any protected children)
    python manage.py delete_validation_runs --workflow-id 4 --confirm

    # Wipe an entire org's run history
    python manage.py delete_validation_runs --org-id 12 --confirm

    # Wipe every run in the database (requires --all + --confirm)
    python manage.py delete_validation_runs --all --confirm
"""

from __future__ import annotations

import logging

from django.apps import apps
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction

from validibot.validations.models import ValidationRun

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Delete validation runs (and their PROTECT-cascaded children)."

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument(
            "--workflow-id",
            type=int,
            help="Delete runs belonging to this workflow id.",
        )
        scope.add_argument(
            "--org-id",
            type=int,
            help="Delete runs belonging to this organisation id.",
        )
        scope.add_argument(
            "--all",
            action="store_true",
            help=(
                "Delete every validation run in the database. "
                "Requires --confirm to proceed."
            ),
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help=(
                "Required to actually delete. Without it the command "
                "runs in dry-run mode and reports counts only."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Force dry-run even if --confirm is passed. Useful for "
                "scripted previews."
            ),
        )

    def handle(self, *args, **options):
        queryset = self._build_queryset(options)
        run_count = queryset.count()
        scope_label = self._scope_label(options)

        if run_count == 0:
            self.stdout.write(f"No validation runs match {scope_label}.")
            return

        # Count PROTECT-cascaded credential children explicitly so the
        # operator sees exactly what will be removed before the delete.
        issued_count, digest_count = self._count_protected_children(queryset)

        self.stdout.write(f"Scope: {scope_label}")
        self.stdout.write(f"  Validation runs to delete: {run_count}")
        self.stdout.write(f"  Issued credentials to delete: {issued_count}")
        self.stdout.write(f"  Credential digest metadata to delete: {digest_count}")

        if options["dry_run"] or not options["confirm"]:
            self.stdout.write(
                self.style.WARNING(
                    "Dry run — no rows deleted. Re-run with --confirm to "
                    "actually delete.",
                ),
            )
            return

        with transaction.atomic():
            issued_deleted = self._delete_protected_children(queryset)
            run_deleted, run_breakdown = queryset.delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {run_deleted} rows total "
                f"({issued_deleted} protected credential rows first, "
                f"then validation runs and their CASCADE children).",
            ),
        )
        for model_label, model_count in sorted(run_breakdown.items()):
            self.stdout.write(f"  {model_label}: {model_count}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_queryset(self, options):
        """Construct the ``ValidationRun`` queryset for the chosen scope."""
        if options["all"]:
            return ValidationRun.objects.all()
        if options["workflow_id"] is not None:
            return ValidationRun.objects.filter(workflow_id=options["workflow_id"])
        if options["org_id"] is not None:
            return ValidationRun.objects.filter(org_id=options["org_id"])
        # The mutually_exclusive_group with required=True should make
        # this branch unreachable, but argparse occasionally lets edge
        # combinations through (e.g. older versions).
        raise CommandError(
            "Must specify exactly one of --workflow-id, --org-id, or --all.",
        )

    def _scope_label(self, options) -> str:
        if options["all"]:
            return "ALL validation runs in the database"
        if options["workflow_id"] is not None:
            return f"workflow id {options['workflow_id']}"
        return f"org id {options['org_id']}"

    def _count_protected_children(self, run_queryset) -> tuple[int, int]:
        """Return (issued_credential_count, digest_metadata_count).

        Returns (0, 0) when ``validibot_pro`` is not installed, since
        the protected child models live in that app.
        """
        if not apps.is_installed("validibot_pro"):
            return 0, 0
        from validibot_pro.credentials.models import IssuedCredential
        from validibot_pro.credentials.models import ValidationCredentialDigestMetadata

        run_ids = list(run_queryset.values_list("id", flat=True))
        issued = IssuedCredential.objects.filter(workflow_run_id__in=run_ids).count()
        digests = ValidationCredentialDigestMetadata.objects.filter(
            workflow_run_id__in=run_ids,
        ).count()
        return issued, digests

    def _delete_protected_children(self, run_queryset) -> int:
        """Remove PROTECT-bound credential rows first, return rows deleted."""
        if not apps.is_installed("validibot_pro"):
            return 0
        from validibot_pro.credentials.models import IssuedCredential
        from validibot_pro.credentials.models import ValidationCredentialDigestMetadata

        run_ids = list(run_queryset.values_list("id", flat=True))
        issued_deleted, _ = IssuedCredential.objects.filter(
            workflow_run_id__in=run_ids,
        ).delete()
        digests_deleted, _ = ValidationCredentialDigestMetadata.objects.filter(
            workflow_run_id__in=run_ids,
        ).delete()
        return issued_deleted + digests_deleted
