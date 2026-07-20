"""Set or clear the emergency block on one exact validator deployment."""

from django.core.management.base import BaseCommand
from django.core.management.base import CommandError

from validibot.validations.models import ValidatorExecutionDeployment
from validibot.validations.services.execution.deployments import (
    set_execution_deployment_block,
)


class Command(BaseCommand):
    """Provide a narrow audited emergency-block operator surface."""

    help = "Block or unblock one validator execution deployment by UUID."

    def add_arguments(self, parser):
        parser.add_argument("deployment_id", help="Exact deployment UUID.")
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument("--block", action="store_true")
        action.add_argument("--unblock", action="store_true")
        parser.add_argument(
            "--reason",
            default="",
            help="Required non-secret operator reason when blocking.",
        )

    def handle(self, *args, **options):
        """Apply the locked service operation and report its resulting state."""
        try:
            deployment = ValidatorExecutionDeployment.objects.get(
                pk=options["deployment_id"]
            )
        except (ValueError, ValidatorExecutionDeployment.DoesNotExist) as exc:
            raise CommandError("Validator deployment was not found.") from exc
        try:
            deployment = set_execution_deployment_block(
                deployment,
                blocked=bool(options["block"]),
                reason=str(options["reason"]),
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        state = "BLOCKED" if deployment.emergency_blocked else "UNBLOCKED"
        self.stdout.write(self.style.SUCCESS(f"{deployment.pk} is now {state}."))
