"""Re-verify registered inactive Services after provider capacity changes.

Release activation cools every superseded Service at the provider before this
command runs. Re-observing by durable resource name avoids reconstructing old
release names and records the verified minimum-instance change in the audit
log while preserving each immutable deployment identity.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from google.api_core.exceptions import GoogleAPICallError
from google.cloud import run_v2
from google.iam.v1 import iam_policy_pb2

from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionDeploymentReadiness
from validibot.validations.constants import ExecutionDeploymentRoutingRole
from validibot.validations.models import ValidatorExecutionDeployment
from validibot.validations.services.execution.gcp_service_import import (
    GCPServiceImportError,
)
from validibot.validations.services.execution.gcp_service_import import (
    observe_cloud_run_service,
)
from validibot.validations.services.execution.gcp_service_import import (
    verify_registered_service_deployment,
)


def _matching_live_revision(deployments, *, revision: str):
    """Return registered rows for the live revision or fail closed."""
    matches = [
        deployment
        for deployment in deployments
        if deployment.deployment_revision == revision
    ]
    if not matches:
        raise GCPServiceImportError(
            "Registered Service resource no longer exposes any known ready revision."
        )
    return matches


class Command(BaseCommand):
    """Verify every ready inactive Service directly from its registered name."""

    help = "Re-verify and audit registered inactive Cloud Run Services."

    def handle(self, *args, **options):
        invoker = str(
            getattr(settings, "GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT", "")
        ).strip()
        if not invoker:
            raise CommandError(
                "GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT is required."
            )
        deployments = list(
            ValidatorExecutionDeployment.objects.filter(
                deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
                readiness_state=ExecutionDeploymentReadiness.READY,
                routing_role=ExecutionDeploymentRoutingRole.INACTIVE,
            )
            .select_related("validator")
            .order_by("provider_resource_name", "validator__slug", "pk")
        )
        grouped: dict[str, list[ValidatorExecutionDeployment]] = {}
        for deployment in deployments:
            grouped.setdefault(deployment.provider_resource_name, []).append(deployment)
        client = run_v2.ServicesClient()
        verified = 0
        try:
            for resource_name, matching_deployments in grouped.items():
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
                for deployment in _matching_live_revision(
                    matching_deployments,
                    revision=observation.revision,
                ):
                    verify_registered_service_deployment(
                        deployment,
                        observation=observation,
                    )
                    verified += 1
        except (GCPServiceImportError, GoogleAPICallError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Verified {verified} inactive Service deployment(s) across "
                f"{len(grouped)} provider resource(s)."
            )
        )
