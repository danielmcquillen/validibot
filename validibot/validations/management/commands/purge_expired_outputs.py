"""
Management command to purge expired validation run outputs.

This command processes validation runs that have passed their output retention
period (output_expires_at < now) and purges their outputs (findings, artifacts,
and storage files). The run record is preserved for audit trail.

This is separate from purge_expired_submissions which handles user-submitted
files. Output retention is typically longer than submission retention since
users need time to review and download validation results.

This command should be scheduled to run periodically (e.g., hourly via
Cloud Scheduler or cron).

Usage:
    python manage.py purge_expired_outputs
    python manage.py purge_expired_outputs --batch-size 100
    python manage.py purge_expired_outputs --dry-run

Environment:
    This command is designed to run on worker instances where it has access
    to storage for deleting run files and artifacts.
"""

import logging

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from validibot.submissions.constants import OutputRetention
from validibot.validations.models import ValidationRun

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Purge outputs from validation runs that have passed their retention period."

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Number of runs to process per batch (default: 100)",
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

            # Find runs with expired outputs that haven't been purged yet
            # Exclude runs with STORE_PERMANENTLY policy (output_expires_at is null)
            expired_runs = (
                ValidationRun.objects.filter(
                    output_expires_at__lte=now,
                    output_purged_at__isnull=True,
                )
                .exclude(
                    output_retention_policy=OutputRetention.STORE_PERMANENTLY,
                )
                .order_by("output_expires_at")[:batch_size]
            )

            # Convert to list to avoid queryset changes during iteration
            runs_to_process = list(expired_runs)

            if not runs_to_process:
                if total_purged == 0 and total_failed == 0:
                    self.stdout.write(
                        self.style.SUCCESS("No expired outputs to purge.")
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
                f"Processing batch {batch_count}: {len(runs_to_process)} run(s)"
            )

            for run in runs_to_process:
                if dry_run:
                    self.stdout.write(
                        f"  [DRY RUN] Would purge outputs: {run.id} "
                        f"(policy={run.output_retention_policy}, "
                        f"expires={run.output_expires_at})"
                    )
                    total_purged += 1
                    continue

                try:
                    with transaction.atomic():
                        self._purge_run_outputs(run)
                    total_purged += 1
                    self.stdout.write(self.style.SUCCESS(f"  Purged outputs: {run.id}"))
                except Exception as e:
                    total_failed += 1
                    self.stdout.write(
                        self.style.ERROR(f"  Failed to purge outputs {run.id}: {e}")
                    )
                    logger.exception(
                        "Failed to purge expired run outputs",
                        extra={"run_id": str(run.id)},
                    )
                    # Continue with other runs - don't stop on failure

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"[DRY RUN] Would have purged outputs for {total_purged} run(s)."
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
                    f"{total_failed} run(s) failed to purge. Check logs and retry."
                )
            )

    def _purge_run_outputs(self, run: ValidationRun) -> None:
        """
        Purge all outputs for a validation run.

        This includes:
        - Findings (ValidationFinding records)
        - Artifacts (Artifact records and their files)
        - Storage files (run directory in data storage)

        The run record and summary are preserved for audit trail.
        """
        from validibot.core.storage import get_data_storage
        from validibot.validations.models import ValidationFinding

        run_id = str(run.id)

        # Delete findings
        findings_count = run.findings.count()
        if findings_count > 0:
            ValidationFinding.objects.filter(validation_run=run).delete()
            logger.info(
                "Deleted findings for run",
                extra={"run_id": run_id, "findings_count": findings_count},
            )

        # Delete artifacts and their files
        artifacts = list(run.artifacts.all())
        for artifact in artifacts:
            try:
                if artifact.file:
                    artifact.file.delete(save=False)
            except Exception:
                logger.exception(
                    "Failed to delete artifact file",
                    extra={"run_id": run_id, "artifact_id": artifact.id},
                )
            artifact.delete()

        if artifacts:
            logger.info(
                "Deleted artifacts for run",
                extra={"run_id": run_id, "artifacts_count": len(artifacts)},
            )

        # Delete run files from data storage
        try:
            storage = get_data_storage()
            org_id = str(run.org_id)
            run_path = f"runs/{org_id}/{run_id}/"
            files_deleted = storage.delete_prefix(run_path)
            if files_deleted > 0:
                logger.info(
                    "Deleted run files from storage",
                    extra={"run_id": run_id, "files_deleted": files_deleted},
                )
        except Exception:
            logger.exception(
                "Failed to delete run files from storage",
                extra={"run_id": run_id},
            )
            raise

        # Mark run as purged
        run.output_purged_at = timezone.now()
        run.output_expires_at = None  # No longer pending
        run.save(update_fields=["output_purged_at", "output_expires_at"])

        logger.info(
            "Purged run outputs",
            extra={
                "run_id": run_id,
                "output_retention_policy": run.output_retention_policy,
            },
        )
