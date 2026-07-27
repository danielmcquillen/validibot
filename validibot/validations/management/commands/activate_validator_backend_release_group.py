"""Activate several accepted backend releases in one database transaction."""

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from validibot.validations.constants import ExecutionDeploymentDeactivationCause
from validibot.validations.constants import ExecutionRoutingMode
from validibot.validations.services.execution.deployments import (
    ExecutionDeploymentResolutionError,
)
from validibot.validations.services.execution.deployments import (
    activate_backend_release_group,
)


class Command(BaseCommand):
    """Expose all-or-nothing setup and multi-backend routing to operators."""

    help = (
        "Activate accepted backend releases as one database transaction. "
        "Repeat --release with backend=version values."
    )

    def add_arguments(self, parser):
        """Accept a bounded routing mode, cause, and repeated release identity."""
        parser.add_argument(
            "--release",
            action="append",
            required=True,
            help="Backend release in backend=version form; repeat as needed.",
        )
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

    def handle(self, *args, **options):
        """Parse unique identities and apply the group transition atomically."""
        releases: dict[str, str] = {}
        for value in options["release"]:
            backend, separator, version = value.partition("=")
            if not separator or not backend or not version:
                raise CommandError("--release must use backend=version")
            if backend in releases:
                raise CommandError(f"Duplicate backend release: {backend}")
            releases[backend] = version
        try:
            activated = activate_backend_release_group(
                releases=releases,
                mode=options["mode"],
                deactivation_cause=options["cause"],
            )
        except (ExecutionDeploymentResolutionError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        for backend, pairs in activated.items():
            self.stdout.write(f"{backend}: activated {len(pairs)} pair(s)")
        self.stdout.write(
            self.style.SUCCESS(
                f"Activated {len(activated)} backend release(s) "
                f"in {options['mode']} mode."
            )
        )
