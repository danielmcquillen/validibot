"""Activate one accepted backend release through pair-aware routing."""

import base64
import binascii

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from validibot.validations.constants import ExecutionDeploymentDeactivationCause
from validibot.validations.constants import ExecutionRoutingMode
from validibot.validations.services.execution.deployments import (
    ExecutionDeploymentResolutionError,
)
from validibot.validations.services.execution.deployments import (
    activate_backend_release,
)


class Command(BaseCommand):
    """Expose the transactional backend-level routing service to operators."""

    help = (
        "Activate every compatible semantic Validator pair for one accepted "
        "backend release. This command does not contact Cloud Run."
    )

    def add_arguments(self, parser):
        """Require exact backend, version, mode, and bounded lifecycle cause."""
        parser.add_argument("--backend", required=True)
        parser.add_argument("--version", required=True)
        parser.add_argument(
            "--mode",
            choices=[
                ExecutionRoutingMode.NORMAL,
                ExecutionRoutingMode.JOB_ONLY,
                ExecutionRoutingMode.INACTIVE,
            ],
            default=ExecutionRoutingMode.NORMAL,
        )
        parser.add_argument(
            "--cause",
            choices=ExecutionDeploymentDeactivationCause.values,
            default=(
                ExecutionDeploymentDeactivationCause.SUPERSEDED_BY_ACCEPTED_RELEASE
            ),
        )
        parser.add_argument(
            "--allow-unaccepted",
            action="store_true",
            help=(
                "Acceptance-only diagnostic: permit temporary private routing "
                "before accepted_at is recorded."
            ),
        )
        parser.add_argument(
            "--reason-b64",
            default="",
            help=(
                "Optional UTF-8 operator reason encoded as strict base64 for "
                "safe transport through the remote management-command wrapper."
            ),
        )

    def handle(self, *args, **options):
        """Apply one all-or-nothing route transition and report exact row IDs."""
        reason = ""
        if options["reason_b64"]:
            try:
                reason = base64.b64decode(
                    options["reason_b64"],
                    validate=True,
                ).decode("utf-8")
            except (binascii.Error, UnicodeDecodeError) as exc:
                raise CommandError("--reason-b64 must encode valid UTF-8 text") from exc
        try:
            pairs = activate_backend_release(
                backend_slug=options["backend"],
                backend_release_identity=options["version"],
                mode=options["mode"],
                deactivation_cause=options["cause"],
                require_accepted=not options["allow_unaccepted"],
                operator_reason=reason,
            )
        except (ExecutionDeploymentResolutionError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        for pair in pairs:
            self.stdout.write(
                f"{pair.service.validator.slug}: "
                f"Service {pair.service.pk}; Job {pair.job.pk}"
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Activated {len(pairs)} pair(s) for {options['backend']} "
                f"{options['version']} in {options['mode']} mode."
            )
        )
