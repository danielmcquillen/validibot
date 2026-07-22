"""Run and persist the private managed-validator acceptance suite.

This command is the application half of ``just gcp validator-acceptance``.
The operator recipe owns the maintenance window, queue state, route activation,
and rollback.  The management command owns deterministic workflow fixtures,
live canaries, durable evidence checks, and the immutable private report.
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from validibot.validations.acceptance import ValidatorAcceptanceRunner
from validibot.validations.acceptance import persist_acceptance_report

MIN_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 3600


class Command(BaseCommand):
    """Exercise all managed backends and emit one automation-friendly result."""

    help = "Run live validator acceptance and persist its private JSON evidence."

    def add_arguments(self, parser):
        """Expose only release identity, burst size, and a bounded wait."""
        parser.add_argument(
            "--release-tag",
            required=True,
            help="Candidate backend release in vX.Y.Z form.",
        )
        parser.add_argument(
            "--attempts",
            type=int,
            default=20,
            help="Concurrent canaries per backend (default: 20).",
        )
        parser.add_argument(
            "--timeout-seconds",
            type=int,
            default=1800,
            help="Maximum wait for each backend burst (default: 1800).",
        )
        parser.add_argument(
            "--skip-storage-probe",
            action="store_true",
            help="Developer-only: skip the live GCS capability check.",
        )
        parser.add_argument(
            "--require-persisted-report",
            action="store_true",
            help="Fail unless the final report is written to private GCS.",
        )
        parser.add_argument(
            "--ambient-isolation-verified",
            action="store_true",
            help="Confirm the operator recipe just proved ambient IAM is absent.",
        )

    def handle(self, *args, **options):
        """Run the suite, persist evidence, print JSON, and fail closed."""
        timeout_seconds = options["timeout_seconds"]
        if not MIN_TIMEOUT_SECONDS <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
            raise CommandError(
                "timeout-seconds must be between "
                f"{MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS}"
            )
        try:
            report = ValidatorAcceptanceRunner(
                release_tag=options["release_tag"],
                attempts_per_backend=options["attempts"],
                timeout_seconds=timeout_seconds,
                run_storage_probe=not options["skip_storage_probe"],
                ambient_isolation_verified=options["ambient_isolation_verified"],
            ).run()
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        document = report.as_dict()
        evidence = None
        persistence_error = ""
        try:
            evidence = persist_acceptance_report(document)
        except Exception as exc:
            persistence_error = f"{type(exc).__name__}: {str(exc)[:400]}"

        output = {
            **document,
            "evidence": evidence,
        }
        if persistence_error:
            output["evidence_error"] = persistence_error
        self.stdout.write(json.dumps(output, indent=2, sort_keys=True))

        if persistence_error:
            raise CommandError("Validator acceptance evidence could not be persisted")
        if options["require_persisted_report"] and evidence is None:
            raise CommandError("Private GCS report persistence is not configured")
        if not report.passed:
            raise CommandError("Validator acceptance failed; see the JSON report")
