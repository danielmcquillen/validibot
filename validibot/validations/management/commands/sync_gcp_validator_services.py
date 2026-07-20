"""Verify live private validator Services and register their immutable routes."""

import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import run_v2
from google.iam.v1 import iam_policy_pb2

from validibot.validations.constants import CLOUD_RUN_SERVICE_MAXIMUM_DOMAIN_SECONDS
from validibot.validations.constants import ValidationType
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

SERVICE_SLUGS = {
    ValidationType.ENERGYPLUS: "energyplus",
    ValidationType.FMU: "fmu",
    ValidationType.SHACL: "shacl",
    ValidationType.SCHEMATRON: "schematron",
}
_RELEASE_TAG = re.compile(r"^v[0-9]+\.[0-9]+\.[0-9]+$")


def _verify_backend_release(
    observation, *, service_name: str, release_tag: str
) -> None:
    """Require the immutable Service name and runtime release to agree."""
    expected_release = release_tag.removeprefix("v")
    if release_tag and observation.backend_release_identity != expected_release:
        raise GCPServiceImportError(
            f"Service {service_name} reports backend release "
            f"{observation.backend_release_identity}, expected {expected_release}."
        )


class Command(BaseCommand):
    """Import exact ready Service revisions without mutating provider state."""

    help = (
        "Verify and register private Cloud Run validator Services. Use "
        "--activate-primary only after retained Job routes are READY."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--activate-primary",
            action="store_true",
            help="Activate each Service and retain the current Job as long-running.",
        )
        parser.add_argument(
            "--maximum-execution-seconds",
            type=int,
            default=CLOUD_RUN_SERVICE_MAXIMUM_DOMAIN_SECONDS,
            help="Verified domain execution ceiling; must be 1..1500.",
        )
        parser.add_argument(
            "--backend-release-tag",
            default="",
            help=(
                "Signed release tag used in immutable Service names, for example "
                "v0.15.0. Empty retains the legacy unsuffixed lookup."
            ),
        )

    def handle(self, *args, **options):
        project_id = str(getattr(settings, "GCP_PROJECT_ID", ""))
        region = str(getattr(settings, "GCP_REGION", ""))
        app_name = str(getattr(settings, "GCP_APP_NAME", "validibot") or "validibot")
        stage = str(getattr(settings, "VALIDIBOT_STAGE", "prod") or "prod")
        invoker = str(
            getattr(settings, "GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT", "")
        )
        maximum_execution_seconds = int(options["maximum_execution_seconds"])
        release_tag = str(options["backend_release_tag"] or "")
        if release_tag and not _RELEASE_TAG.fullmatch(release_tag):
            raise CommandError("--backend-release-tag must be vX.Y.Z.")
        release_label = release_tag.replace(".", "-")
        if not project_id or not region or not invoker:
            raise CommandError(
                "GCP_PROJECT_ID, GCP_REGION, and the validator task invoker are "
                "required."
            )
        if not (
            1 <= maximum_execution_seconds <= CLOUD_RUN_SERVICE_MAXIMUM_DOMAIN_SECONDS
        ):
            raise CommandError("--maximum-execution-seconds must be 1..1500.")
        validators = Validator.objects.filter(
            validation_type__in=SERVICE_SLUGS,
            is_enabled=True,
            release_state=ValidatorReleaseState.PUBLISHED,
            availability_state=ValidatorAvailabilityState.AVAILABLE,
        ).order_by("validation_type", "pk")
        validators_by_type: dict[str, list[Validator]] = {}
        for validator in validators:
            validators_by_type.setdefault(validator.validation_type, []).append(
                validator
            )
        client = run_v2.ServicesClient()
        created_count = 0
        verified_count = 0
        try:
            for validation_type, matching_validators in validators_by_type.items():
                service_name = (
                    f"{app_name}-validator-service-"
                    f"{SERVICE_SLUGS[ValidationType(validation_type)]}"
                )
                if release_label:
                    service_name = f"{service_name}-{release_label}"
                if stage != "prod":
                    service_name = f"{service_name}-{stage}"
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
                _verify_backend_release(
                    observation,
                    service_name=service_name,
                    release_tag=release_tag,
                )
                for validator in matching_validators:
                    deployment, created = register_observed_service_deployment(
                        validator=validator,
                        project_id=project_id,
                        region=region,
                        observation=observation,
                        maximum_execution_seconds=maximum_execution_seconds,
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
