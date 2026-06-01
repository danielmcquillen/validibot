"""Completely reset a Validibot deployment to a clean baseline.

This command is the "factory reset" for a deployed environment. It wipes the
operational data an instance accumulates — validation runs, submissions,
workflows, and projects — and rebuilds the system validator catalogue from the
current code, then re-seeds validator resource files.

What it deletes
---------------

1. **Validation runs** (and their PROTECT-cascaded children). Delegated to
   ``delete_validation_runs --all --confirm`` so the Pro credential rows
   (``IssuedCredential``, ``ValidationCredentialDigestMetadata``) that use
   ``on_delete=PROTECT`` are removed first — a naive ``ValidationRun.delete()``
   would raise ``ProtectedError`` on any workflow that ran the signing action.
2. **Submissions** (and their uploaded files / ``PurgeRetry`` rows).
3. **Workflows** (cascading their steps and step resources).
4. **Projects** (cascading anything project-owned that survived the above).
5. **Validators** (cascading ``CustomValidator``, ``StepIODefinition``,
   ``Derivation``, and ``ValidatorResourceFile``), then recreated from the
   current ``ValidatorConfig`` declarations at the versions those configs
   declare — the genuine "latest specifications".

What it preserves
-----------------

Users and organizations are NOT touched. Neither is anything not listed above.

Deletion order matters
----------------------

The order above is forced by the foreign-key ``PROTECT`` graph, not chosen for
convenience. ``ValidationRun.workflow`` and ``Submission.workflow`` are
``PROTECT``, so runs and submissions must die before workflows.
``WorkflowStep.validator`` is ``PROTECT``, so workflows (and their cascaded
steps) must die before validators. Running these deletes in any other order
raises ``ProtectedError`` and aborts the whole transaction.

Why a confirmation argument and not just a prompt
-------------------------------------------------

On GCP this runs as a non-interactive Cloud Run Job
(``just gcp management-cmd <stage> "reset_system ..."``) with no TTY — a bare
``input()`` prompt would hit ``EOFError`` and crash. So the real gate is a
required ``--confirm`` phrase passed as an argument. When the command *does*
detect an interactive terminal (local shell, attached container), it adds a
second live prompt as a courtesy. Both gates protect the same thing: nothing is
deleted unless the operator typed the exact phrase.

Safety
------

- Dry-run is the **default**: with no (or a wrong) ``--confirm`` phrase the
  command prints the deletion plan and exits without touching data.
- A *wrong* ``--confirm`` value is treated as an error (non-zero exit), so a
  typo can never be mistaken for consent.
- Database deletes and the validator rebuild run in a single
  ``transaction.atomic()`` block — if the rebuild fails, the deletes roll back
  and the instance is left exactly as it was.
- Storage (GCS/S3/local) blob purges are irreversible and therefore run *after*
  the database transaction commits, using paths captured *before* deletion.

Examples
--------

::

    # Preview only — counts every entity, deletes nothing
    just gcp reset-system prod
    python manage.py reset_system

    # Actually wipe and rebuild
    just gcp reset-system prod confirm
    python manage.py reset_system --confirm RESET-EVERYTHING
"""

from __future__ import annotations

import logging
import os
import sys

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction

from validibot.projects.models import Project
from validibot.submissions.models import Submission
from validibot.validations.models import ValidationRun
from validibot.validations.models import Validator
from validibot.validations.utils import create_default_validators
from validibot.workflows.models import Workflow

logger = logging.getLogger(__name__)

# The exact phrase an operator must type to authorise a wipe. Deliberately a
# single hyphenated token so it survives the nested quoting of
# ``just gcp management-cmd <stage> "reset_system --confirm RESET-EVERYTHING"``
# without needing escaped inner spaces.
CONFIRM_PHRASE = "RESET-EVERYTHING"


class Command(BaseCommand):
    help = (
        "DESTRUCTIVE: delete all runs, submissions, workflows, projects and "
        "validators, then rebuild the validator catalogue. Dry-run by default; "
        f'requires --confirm "{CONFIRM_PHRASE}" to actually delete.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            default="",
            metavar="PHRASE",
            help=(
                f'Must equal "{CONFIRM_PHRASE}" to perform the reset. Without '
                "it (or with any other value) the command runs in dry-run "
                "mode and deletes nothing."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Force dry-run even when the correct --confirm phrase is "
                "given. Useful for scripted previews."
            ),
        )
        parser.add_argument(
            "--noinput",
            "--no-input",
            action="store_false",
            dest="interactive",
            default=True,
            help=(
                "Skip the extra interactive confirmation prompt that fires "
                "when a real terminal is attached. The --confirm phrase is "
                "still required. Implied on non-interactive runners like "
                "Cloud Run Jobs, which have no TTY."
            ),
        )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def handle(self, *args, **options):
        confirm = options["confirm"]
        force_dry_run = options["dry_run"]
        interactive = options["interactive"]

        counts = self._gather_counts()
        self._print_plan(counts)

        phrase_ok = confirm == CONFIRM_PHRASE

        # A *wrong* phrase is an error, never a silent dry-run — a typo must
        # not be quietly downgraded to "did nothing" and mistaken for success.
        if confirm and not phrase_ok:
            msg = (
                f'Confirmation phrase mismatch. Pass --confirm "{CONFIRM_PHRASE}" '
                "exactly to proceed."
            )
            raise CommandError(msg)

        if force_dry_run or not phrase_ok:
            self.stdout.write(
                self.style.WARNING(
                    "DRY RUN — nothing was deleted. Re-run with "
                    f'--confirm "{CONFIRM_PHRASE}" to perform the reset.',
                ),
            )
            return

        # Correct phrase + not forced dry-run. When a terminal is present, ask
        # once more interactively. On Cloud Run there is no TTY, so this is
        # skipped and the --confirm argument is the sole gate.
        if interactive and sys.stdin.isatty():
            self._interactive_gate()

        # Capture irreversible storage references BEFORE any row disappears.
        run_prefixes, submission_files = self._capture_storage_paths()

        # Database work is atomic: delete everything and rebuild validators, or
        # roll the whole thing back leaving the instance untouched.
        with transaction.atomic():
            self._delete_all()
            self._recreate_validators()

        # Only now, after a successful commit, purge blobs. Doing this earlier
        # would orphan-delete files for a transaction that might still roll back.
        self._purge_storage(run_prefixes, submission_files)

        # Best-effort resource re-seed. Runs outside the transaction because a
        # missing weather-data directory on a minimal image must not undo the
        # reset that already committed.
        self._reseed_resources()

        self.stdout.write(self.style.SUCCESS("System reset complete."))

    # ------------------------------------------------------------------
    # Planning / reporting
    # ------------------------------------------------------------------

    def _gather_counts(self) -> dict[str, int]:
        """Count every entity the reset will remove, for the plan preview."""
        return {
            "validation runs": ValidationRun.objects.count(),
            "submissions": Submission.objects.count(),
            "workflows": Workflow.objects.count(),
            "projects": Project.objects.count(),
            "validators": Validator.objects.count(),
        }

    def _print_plan(self, counts: dict[str, int]) -> None:
        """Show the operator exactly which instance and rows are in scope.

        The environment/database banner is the main guard against the classic
        "I thought I was on dev" disaster — even though the confirmation phrase
        itself is environment-agnostic, the operator sees the target before
        anything happens.
        """
        db = settings.DATABASES.get("default", {})
        db_name = db.get("NAME", "?")
        db_host = db.get("HOST") or "(local socket)"
        settings_module = os.environ.get("DJANGO_SETTINGS_MODULE", "?")

        self.stdout.write(self.style.MIGRATE_HEADING("Validibot system reset"))
        self.stdout.write(f"  Settings module: {settings_module}")
        self.stdout.write(f"  Database:        {db_name} @ {db_host}")
        self.stdout.write("  Will DELETE:")
        for label, count in counts.items():
            self.stdout.write(f"    - {count} {label}")
        self.stdout.write(
            "  Will REBUILD: system validators (from current configs) + resource files",
        )
        self.stdout.write("  Will PRESERVE: users, organizations")

    def _interactive_gate(self) -> None:
        """Second, live confirmation when a TTY is attached.

        Skipped automatically on non-interactive runners (Cloud Run Jobs),
        where ``sys.stdin.isatty()`` is False and the ``--confirm`` argument is
        the only gate.
        """
        self.stdout.write(
            self.style.WARNING(
                "\nThis permanently deletes the data listed above and cannot "
                "be undone.",
            ),
        )
        typed = input(f'Type "{CONFIRM_PHRASE}" to proceed: ').strip()
        if typed != CONFIRM_PHRASE:
            raise CommandError("Interactive confirmation did not match. Aborted.")

    # ------------------------------------------------------------------
    # Storage capture / purge
    # ------------------------------------------------------------------

    def _capture_storage_paths(self) -> tuple[list[str], list[str]]:
        """Collect blob locations before their owning rows are deleted.

        Run output bundles live under ``runs/{org_id}/{run_id}/`` in the data
        storage backend; submission uploads live in the ``input_file`` field's
        storage. We snapshot the path strings now so we can purge them after the
        database transaction commits, when the rows themselves are gone.
        """
        run_prefixes = [
            f"runs/{org_id}/{run_id}/"
            for run_id, org_id in ValidationRun.objects.values_list("id", "org_id")
        ]
        submission_files = [
            name
            for name in Submission.objects.exclude(input_file="")
            .exclude(input_file__isnull=True)
            .values_list("input_file", flat=True)
            if name
        ]
        return run_prefixes, submission_files

    def _purge_storage(
        self,
        run_prefixes: list[str],
        submission_files: list[str],
    ) -> None:
        """Delete the captured blobs. Irreversible; runs post-commit only.

        Failures here are logged but never raised: the database reset already
        succeeded, and a leftover blob is a re-runnable annoyance, not a reason
        to present the whole operation as failed.
        """
        from validibot.core.storage import get_data_storage

        purged = 0
        data_storage = get_data_storage()
        for prefix in run_prefixes:
            try:
                purged += data_storage.delete_prefix(prefix)
            except Exception:
                logger.exception("Failed to purge run prefix %s", prefix)

        # Submission uploads use the FileField's own storage, which may differ
        # from the data storage used for run bundles.
        file_storage = Submission._meta.get_field("input_file").storage
        for name in submission_files:
            try:
                file_storage.delete(name)
                purged += 1
            except Exception:
                logger.exception("Failed to purge submission file %s", name)

        self.stdout.write(f"  Purged {purged} storage object(s).")

    # ------------------------------------------------------------------
    # Deletion / rebuild
    # ------------------------------------------------------------------

    def _delete_all(self) -> None:
        """Delete the in-scope rows in PROTECT-safe order.

        Run deletion is delegated to ``delete_validation_runs`` so the Pro
        credential PROTECT children are handled in one place rather than
        duplicated here.
        """
        # Runs first: they PROTECT both workflows, and the delegated command
        # clears the Pro credential rows that PROTECT the runs themselves.
        call_command(
            "delete_validation_runs",
            all=True,
            confirm=True,
            stdout=self.stdout,
        )

        sub_deleted, _ = Submission.objects.all().delete()
        self.stdout.write(f"  Deleted {sub_deleted} submission row(s).")

        # Workflows cascade to their steps, releasing the PROTECT references
        # that steps hold on validators.
        wf_deleted, _ = Workflow.objects.all().delete()
        self.stdout.write(f"  Deleted {wf_deleted} workflow row(s).")

        proj_deleted, _ = Project.objects.all().delete()
        self.stdout.write(f"  Deleted {proj_deleted} project row(s).")

        # Validators last: now that no step references them, they (and their
        # CustomValidator / StepIODefinition / Derivation / ValidatorResourceFile
        # children) delete cleanly.
        val_deleted, _ = Validator.objects.all().delete()
        self.stdout.write(f"  Deleted {val_deleted} validator row(s).")

    def _recreate_validators(self) -> None:
        """Rebuild the system validator catalogue from current code.

        Mirrors what ``setup_validibot`` does on a fresh install: the hardcoded
        baseline list followed by config-driven sync, which together create
        every system validator at the version its ``ValidatorConfig`` declares
        plus its signal and derivation definitions.
        """
        created, _updated = create_default_validators()
        self.stdout.write(f"  Recreated {created} baseline validator(s).")
        # Mirror setup_validibot's fresh-install path. --allow-drift is a no-op
        # here (every row was just created, so there is no prior digest to
        # disagree with) but it keeps this call identical to the proven one and
        # immune to any edge where the baseline list and a config briefly
        # diverge on the same (slug, version).
        call_command("sync_validators", "--allow-drift", stdout=self.stdout)

    def _reseed_resources(self) -> None:
        """Re-seed validator resource files (e.g. weather data).

        Deleting validators cascades away their ``ValidatorResourceFile`` rows,
        which ``sync_validators`` does not recreate. This restores the seedable
        ones. Best-effort: a minimal image without the weather-data directory
        must not turn an otherwise-successful reset into a failure.
        """
        try:
            call_command("seed_weather_files", stdout=self.stdout)
        except Exception as exc:
            self.stdout.write(
                self.style.WARNING(
                    f"  Skipped resource re-seed (seed_weather_files): {exc}. "
                    "Re-run it manually if this instance relies on those files.",
                ),
            )
            logger.warning("seed_weather_files failed during reset: %s", exc)
