"""Import live Cloud Run Jobs as verified validator execution deployments."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import run_v2

from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorAvailabilityState
from validibot.validations.constants import ValidatorReleaseState
from validibot.validations.models import Validator
from validibot.validations.services.cloud_run.launcher import (
    _resolve_cloud_run_job_name,
)
from validibot.validations.services.execution.gcp_job_import import GCPJobImportError
from validibot.validations.services.execution.gcp_job_import import (
    observe_cloud_run_job,
)
from validibot.validations.services.execution.gcp_job_import import (
    register_observed_job_deployment,
)

MANAGED_VALIDATION_TYPES = (
    ValidationType.ENERGYPLUS,
    ValidationType.FMU,
    ValidationType.SHACL,
    ValidationType.SCHEMATRON,
)


class Command(BaseCommand):
    """Synchronize exact live Job facts without modifying provider resources."""

    help = (
        "Import digest-pinned Cloud Run Jobs as verified execution deployments. "
        "Historical attempts are intentionally left unlinked."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--activate-primary",
            action="store_true",
            help="Activate each imported Job as the primary route for new attempts.",
        )

    def handle(self, *args, **options):
        project_id = str(getattr(settings, "GCP_PROJECT_ID", ""))
        region = str(getattr(settings, "GCP_REGION", ""))
        if not project_id or not region:
            raise CommandError("GCP_PROJECT_ID and GCP_REGION are required.")
        validators = Validator.objects.filter(
            validation_type__in=MANAGED_VALIDATION_TYPES,
            is_enabled=True,
            release_state=ValidatorReleaseState.PUBLISHED,
            availability_state=ValidatorAvailabilityState.AVAILABLE,
        ).order_by("validation_type", "pk")
        validators_by_type: dict[str, list[Validator]] = {}
        for validator in validators:
            validators_by_type.setdefault(validator.validation_type, []).append(
                validator
            )
        client = run_v2.JobsClient()
        created_count = 0
        verified_count = 0
        try:
            for validation_type, matching_validators in validators_by_type.items():
                job_name = _resolve_cloud_run_job_name(validation_type)
                resource_name = (
                    f"projects/{project_id}/locations/{region}/jobs/{job_name}"
                )
                job = client.get_job(name=resource_name)
                observation = observe_cloud_run_job(
                    job,
                    expected_resource_name=resource_name,
                )
                for validator in matching_validators:
                    deployment, created = register_observed_job_deployment(
                        validator=validator,
                        project_id=project_id,
                        region=region,
                        observation=observation,
                        activate_primary=bool(options["activate_primary"]),
                    )
                    created_count += int(created)
                    verified_count += 1
                    action = "created" if created else "verified"
                    self.stdout.write(
                        f"{action}: {validator.slug} -> "
                        f"{deployment.provider_resource_name}"
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
                f"{created_count}. Historical attempts were not modified."
            )
        )
