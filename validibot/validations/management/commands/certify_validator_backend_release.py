"""Import and accept one immutable validator Job/Service release pair.

This command co-locates the database-side import, temporary route changes, and
small Service and Job canary bursts inside one remote management execution so
operators do not pay Cloud Run Job startup overhead for every transition.
"""

from __future__ import annotations

import re

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from validibot.validations.acceptance import ROUTINE_ACCEPTANCE_ATTEMPTS
from validibot.validations.constants import ExecutionDeploymentDeactivationCause
from validibot.validations.constants import ExecutionRoutingMode


class Command(BaseCommand):
    """Verify and accept one exact backend release as one fail-closed unit."""

    help = "Import and accept one immutable validator Job/Service release pair."

    def add_arguments(self, parser):
        """Require exact provider names and the bounded release-smoke policy."""
        parser.add_argument("--backend", required=True)
        parser.add_argument("--release-tag", required=True)
        parser.add_argument("--job-name", required=True)
        parser.add_argument("--service-name", required=True)
        parser.add_argument("--outgoing-version", default="")
        parser.add_argument("--outgoing-job-name", default="")
        parser.add_argument("--outgoing-service-name", default="")
        parser.add_argument(
            "--attempts",
            type=int,
            default=ROUTINE_ACCEPTANCE_ATTEMPTS,
        )
        parser.add_argument("--timeout-seconds", type=int, default=3000)
        parser.add_argument(
            "--final-mode",
            choices=[ExecutionRoutingMode.NORMAL, ExecutionRoutingMode.INACTIVE],
            default=ExecutionRoutingMode.NORMAL,
            help="Route retained after both acceptance phases pass.",
        )

    def handle(self, *args, **options):
        """Import, exercise both shapes, and leave one explicit final route."""
        backend = str(options["backend"])
        release_tag = str(options["release_tag"])
        match = re.fullmatch(
            rf"{re.escape(backend)}-v"
            r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+"
            r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)",
            release_tag,
        )
        if match is None:
            raise CommandError("release-tag must use <backend>-v<version>")
        release_version = match.group("version")
        route_options = {
            "backend": backend,
            "release_version": release_version,
        }
        outgoing_values = (
            str(options["outgoing_version"]),
            str(options["outgoing_job_name"]),
            str(options["outgoing_service_name"]),
        )
        if any(outgoing_values) and not all(outgoing_values):
            raise CommandError(
                "outgoing-version, outgoing-job-name, and outgoing-service-name "
                "must be supplied together"
            )

        try:
            self.stdout.write("Importing and verifying the immutable provider pair...")
            call_command(
                "sync_gcp_validator_deployments",
                backend=backend,
                job_name=options["job_name"],
                stdout=self.stdout,
                stderr=self.stderr,
            )
            call_command(
                "sync_gcp_validator_services",
                backend=backend,
                service_name=options["service_name"],
                stdout=self.stdout,
                stderr=self.stderr,
            )

            self.stdout.write("Acceptance phase 1/2: Service canaries.")
            call_command(
                "activate_validator_backend_release",
                **route_options,
                mode=ExecutionRoutingMode.NORMAL,
                cause=(
                    ExecutionDeploymentDeactivationCause.SUPERSEDED_BY_ACCEPTED_RELEASE
                ),
                allow_unaccepted=True,
                stdout=self.stdout,
                stderr=self.stderr,
            )
            call_command(
                "run_validator_acceptance",
                backend=backend,
                release_tag=release_tag,
                routing_mode=ExecutionRoutingMode.NORMAL,
                attempts=options["attempts"],
                timeout_seconds=options["timeout_seconds"],
                require_persisted_report=True,
                ambient_isolation_verified=True,
                stdout=self.stdout,
                stderr=self.stderr,
            )

            self.stdout.write("Acceptance phase 2/2: fallback Job canaries.")
            call_command(
                "activate_validator_backend_release",
                **route_options,
                mode=ExecutionRoutingMode.JOB_ONLY,
                cause=ExecutionDeploymentDeactivationCause.SHAPE_ROLLBACK,
                allow_unaccepted=True,
                stdout=self.stdout,
                stderr=self.stderr,
            )
            call_command(
                "run_validator_acceptance",
                backend=backend,
                release_tag=release_tag,
                routing_mode=ExecutionRoutingMode.JOB_ONLY,
                record_acceptance=True,
                attempts=options["attempts"],
                timeout_seconds=options["timeout_seconds"],
                require_persisted_report=True,
                skip_storage_probe=True,
                ambient_isolation_verified=True,
                stdout=self.stdout,
                stderr=self.stderr,
            )

            if all(outgoing_values):
                self.stdout.write(
                    "Reverifying and round-tripping the outgoing rollback pair."
                )
                call_command(
                    "sync_gcp_validator_deployments",
                    backend=backend,
                    job_name=options["outgoing_job_name"],
                    stdout=self.stdout,
                    stderr=self.stderr,
                )
                call_command(
                    "sync_gcp_validator_services",
                    backend=backend,
                    service_name=options["outgoing_service_name"],
                    stdout=self.stdout,
                    stderr=self.stderr,
                )
                call_command(
                    "activate_validator_backend_release",
                    backend=backend,
                    release_version=options["outgoing_version"],
                    mode=ExecutionRoutingMode.NORMAL,
                    cause=(ExecutionDeploymentDeactivationCause.OPERATOR_DEACTIVATION),
                    stdout=self.stdout,
                    stderr=self.stderr,
                )
                call_command(
                    "activate_validator_backend_release",
                    **route_options,
                    mode=ExecutionRoutingMode.NORMAL,
                    cause=(ExecutionDeploymentDeactivationCause.OPERATOR_DEACTIVATION),
                    stdout=self.stdout,
                    stderr=self.stderr,
                )

            call_command(
                "activate_validator_backend_release",
                **route_options,
                mode=options["final_mode"],
                cause=(
                    ExecutionDeploymentDeactivationCause.OPERATOR_DEACTIVATION
                    if all(outgoing_values)
                    else ExecutionDeploymentDeactivationCause.SHAPE_ROLLBACK
                ),
                stdout=self.stdout,
                stderr=self.stderr,
            )
        except Exception:
            self.stderr.write(
                "Acceptance failed; parking the candidate release as inactive."
            )
            try:
                call_command(
                    "activate_validator_backend_release",
                    **route_options,
                    mode=ExecutionRoutingMode.INACTIVE,
                    cause=ExecutionDeploymentDeactivationCause.ACCEPTANCE_FAILURE,
                    allow_unaccepted=True,
                    stdout=self.stdout,
                    stderr=self.stderr,
                )
            except Exception as cleanup_error:
                self.stderr.write(f"Candidate cleanup also failed: {cleanup_error}")
            raise

        self.stdout.write(
            self.style.SUCCESS(
                f"Accepted {release_tag}; final route is {options['final_mode']}."
            )
        )
