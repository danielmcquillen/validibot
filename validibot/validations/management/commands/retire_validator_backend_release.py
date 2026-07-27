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

    help = "Retire one accepted, drained backend release after both resources vanish."

    def add_arguments(self, parser):
        """Require exact identity and the auditable retirement reason."""
        parser.add_argument("--backend", required=True)
        parser.add_argument("--version", required=True)
        parser.add_argument("--reason", required=True)

    def handle(self, *args, **options):
        """Apply final retirement facts to every compatible semantic row."""
        try:
            deployments = retire_backend_release_deployments(
                backend_slug=options["backend"],
                backend_release_identity=options["version"],
                reason=options["reason"],
            )
        except (ExecutionDeploymentResolutionError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(f"Retired {len(deployments)} deployment row(s).")
        )
