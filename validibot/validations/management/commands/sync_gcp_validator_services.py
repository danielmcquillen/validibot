"""Verify one private release-specific Service and register immutable routes."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import run_v2
from google.iam.v1 import iam_policy_pb2

from validibot.validations.constants import CLOUD_RUN_SERVICE_MAXIMUM_DOMAIN_SECONDS
from validibot.validations.constants import ValidatorAvailabilityState
from validibot.validations.constants import ValidatorReleaseState
from validibot.validations.models import Validator
from validibot.validations.services.execution.gcp_service_import import (
    GCPServiceImportError,
)
from validibot.validations.services.execution.gcp_service_import import (
    observe_cloud_run_service,
)
from validibot.validations.services.execution.gcp_service_import import (
    register_observed_service_deployment,
)


def _require_backend(observation, *, expected_backend: str) -> None:
    """Reject a provider resource that declares another backend."""
    if observation.backend_slug != expected_backend:
        raise GCPServiceImportError(
            f"Service reports backend {observation.backend_slug!r}, expected "
            f"{expected_backend!r}."
        )


class Command(BaseCommand):
    """Import one exact ready Service without mutating provider state."""

    help = (
        "Verify and register one private Cloud Run validator Service. "
        "Routing remains unchanged until pair acceptance and activation."
    )

    def add_arguments(self, parser):
        parser.add_argument("--backend", required=True)
        parser.add_argument(
            "--service-name",
            required=True,
            help="Exact release-specific Cloud Run Service name to observe.",
        )
        parser.add_argument(
            "--maximum-execution-seconds",
            type=int,
            default=CLOUD_RUN_SERVICE_MAXIMUM_DOMAIN_SECONDS,
            help="Verified domain execution ceiling; must be 1..1500.",
        )

    def handle(self, *args, **options):
        project_id = str(getattr(settings, "GCP_PROJECT_ID", ""))
        region = str(getattr(settings, "GCP_REGION", ""))
        invoker = str(
            getattr(settings, "GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT", "")
        )
        maximum_execution_seconds = int(options["maximum_execution_seconds"])
        backend_slug = str(options["backend"])
        service_name = str(options["service_name"])
        if not project_id or not region or not invoker:
            raise CommandError(
                "GCP_PROJECT_ID, GCP_REGION, and the validator task invoker are "
                "required."
            )
        if not (
            1 <= maximum_execution_seconds <= CLOUD_RUN_SERVICE_MAXIMUM_DOMAIN_SECONDS
        ):
            raise CommandError("--maximum-execution-seconds must be 1..1500.")
        validators = list(
            Validator.objects.filter(
                execution_backend_slug=backend_slug,
                is_enabled=True,
                release_state=ValidatorReleaseState.PUBLISHED,
                availability_state=ValidatorAvailabilityState.AVAILABLE,
            ).order_by("pk")
        )
        if not validators:
            raise CommandError(
                f"No published compatible Validator declares backend {backend_slug!r}."
            )
        client = run_v2.ServicesClient()
        created_count = 0
        verified_count = 0
        try:
            resource_name = (
                f"projects/{project_id}/locations/{region}/services/{service_name}"
            )
            service = client.get_service(name=resource_name)
            policy = client.get_iam_policy(
                request=iam_policy_pb2.GetIamPolicyRequest(resource=resource_name)
            )
            observation = observe_cloud_run_service(
                service,
                policy=policy,
                expected_resource_name=resource_name,
                invoker_service_account=invoker,
            )
            _require_backend(observation, expected_backend=backend_slug)
            for validator in validators:
                deployment, created = register_observed_service_deployment(
                    validator=validator,
                    project_id=project_id,
                    region=region,
                    observation=observation,
                    maximum_execution_seconds=maximum_execution_seconds,
                    activate_primary=False,
                )
                created_count += int(created)
                verified_count += 1
                action = "created" if created else "verified"
                self.stdout.write(
                    f"{action}: {validator.slug} -> {deployment.provider_resource_name}"
                )
        except (
            GCPServiceImportError,
            GoogleAPICallError,
            KeyError,
            ValidationError,
        ) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Verified {verified_count} Service routes; created {created_count}."
            )
        )
