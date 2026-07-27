"""Import live Cloud Run Jobs as verified validator execution deployments."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import run_v2

from validibot.validations.constants import ValidatorAvailabilityState
from validibot.validations.constants import ValidatorReleaseState
from validibot.validations.models import Validator
from validibot.validations.services.execution.gcp_job_import import GCPJobImportError
from validibot.validations.services.execution.gcp_job_import import (
    observe_cloud_run_job,
)
from validibot.validations.services.execution.gcp_job_import import (
    register_observed_job_deployment,
)


def _require_backend(observation, *, expected_backend: str) -> None:
    """Reject a provider resource that declares another backend."""
    if observation.backend_slug != expected_backend:
        raise GCPJobImportError(
            f"Job reports backend {observation.backend_slug!r}, expected "
            f"{expected_backend!r}."
        )


class Command(BaseCommand):
    """Synchronize one exact release-specific Job without changing routes."""

    help = (
        "Import digest-pinned Cloud Run Jobs as verified execution deployments. "
        "The backend and Job name are explicit; historical attempts and routes "
        "are not modified."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--backend",
            required=True,
            help="Managed backend slug declared by compatible Validator rows.",
        )
        parser.add_argument(
            "--job-name",
            required=True,
            help="Exact release-specific Cloud Run Job name to observe.",
        )

    def handle(self, *args, **options):
        project_id = str(getattr(settings, "GCP_PROJECT_ID", ""))
        region = str(getattr(settings, "GCP_REGION", ""))
        if not project_id or not region:
            raise CommandError("GCP_PROJECT_ID and GCP_REGION are required.")
        backend_slug = str(options["backend"])
        job_name = str(options["job_name"])
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
        client = run_v2.JobsClient()
        created_count = 0
        verified_count = 0
        try:
            resource_name = f"projects/{project_id}/locations/{region}/jobs/{job_name}"
            job = client.get_job(name=resource_name)
            observation = observe_cloud_run_job(
                job,
                expected_resource_name=resource_name,
            )
            _require_backend(observation, expected_backend=backend_slug)
            for validator in validators:
                deployment, created = register_observed_job_deployment(
                    validator=validator,
                    project_id=project_id,
                    region=region,
                    observation=observation,
                    activate_primary=False,
                )
                created_count += int(created)
                verified_count += 1
                action = "created" if created else "verified"
                self.stdout.write(
                    f"{action}: {validator.slug} -> {deployment.provider_resource_name}"
                )
        except (
            GCPJobImportError,
            GoogleAPICallError,
            KeyError,
            ValidationError,
        ) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Verified {verified_count} validator routes; created "
                f"{created_count}. Routing and historical attempts were not modified."
            )
        )
