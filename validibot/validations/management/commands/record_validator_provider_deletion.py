"""Record confirmed deletion of one exact validator provider resource."""

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from validibot.validations.models import ValidatorExecutionDeployment
from validibot.validations.services.execution.deployments import (
    ExecutionDeploymentResolutionError,
)
from validibot.validations.services.execution.deployments import (
    record_execution_deployment_provider_deleted,
)


class Command(BaseCommand):
    """Persist one resumable cleanup checkpoint after provider absence."""

    help = "Record provider_deleted_at for every row naming one exact resource."

    def add_arguments(self, parser):
        """Require the complete canonical resource name."""
        parser.add_argument("--resource", required=True)

    def handle(self, *args, **options):
        """Update matching semantic rows without deleting historical identity."""
        deployments = list(
            ValidatorExecutionDeployment.objects.filter(
                provider_resource_name=options["resource"]
            ).order_by("pk")
        )
        if not deployments:
            raise CommandError("No deployment row names that provider resource.")
        try:
            for deployment in deployments:
                record_execution_deployment_provider_deleted(deployment)
        except ExecutionDeploymentResolutionError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Recorded provider deletion on {len(deployments)} row(s)."
            )
        )
