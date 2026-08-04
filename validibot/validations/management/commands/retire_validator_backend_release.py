"""Retire all historical deployment rows for one deleted backend pair."""

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from validibot.validations.services.execution.deployments import (
    ExecutionDeploymentResolutionError,
)
from validibot.validations.services.execution.deployments import (
    retire_backend_release_deployments,
)


class Command(BaseCommand):
    """Expose final pair retirement after resumable provider deletion."""

    help = "Retire one drained backend release after both resources vanish."

    def add_arguments(self, parser):
        """Require exact identity and the auditable retirement reason."""
        parser.add_argument("--backend", required=True)
        parser.add_argument(
            "--release-version",
            required=True,
            help="Exact semantic backend release version to retire.",
        )
        parser.add_argument("--reason", required=True)
        parser.add_argument(
            "--immediate",
            action="store_true",
            help=(
                "Allow the no-user bootstrap cleanup to retire immediately "
                "after all attempts are proven terminal."
            ),
        )
        parser.add_argument(
            "--allow-unaccepted-candidate",
            action="store_true",
            help=(
                "Allow the immediate empty-installation path to retire a complete, "
                "wholly unaccepted candidate pair after failed private acceptance."
            ),
        )

    def handle(self, *args, **options):
        """Apply final retirement facts to every compatible semantic row."""
        try:
            deployments = retire_backend_release_deployments(
                backend_slug=options["backend"],
                backend_release_identity=options["release_version"],
                reason=options["reason"],
                allow_immediate=options["immediate"],
                drain_days=0 if options["immediate"] else 7,
                allow_unaccepted_candidate=options["allow_unaccepted_candidate"],
            )
        except (ExecutionDeploymentResolutionError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(f"Retired {len(deployments)} deployment row(s).")
        )
