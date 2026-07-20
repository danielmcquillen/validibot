"""Detect drift in active Cloud Run validator Service deployments.

The command is intentionally read-only. It re-observes every primary Service
using the same strict verifier used at registration and compares those live
facts with the immutable deployment snapshot. Cloud Scheduler runs it hourly
so IAM, image, revision, runtime, timeout, and capacity drift become an
operator-visible failure instead of waiting for the next manual rollout.
"""

from __future__ import annotations

import json
import logging

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
    registered_service_observation_mismatches,
)

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    """Verify active Service routes without mutating provider or database state."""

    help = "Verify that primary Cloud Run validator Services have not drifted."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit a stable machine-readable result.",
        )

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
                routing_role=ExecutionDeploymentRoutingRole.PRIMARY,
                emergency_blocked=False,
            )
            .select_related("validator")
            .order_by("validator__slug", "pk")
        )
        client = run_v2.ServicesClient()
        results = []
        try:
            for deployment in deployments:
                resource_name = deployment.provider_resource_name
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
                mismatches = registered_service_observation_mismatches(
                    deployment,
                    observation,
                )
                result = {
                    "deployment_id": str(deployment.pk),
                    "validator": deployment.validator.slug,
                    "provider_resource_name": resource_name,
                    "drifted_fields": mismatches,
                    "verified": not mismatches,
                }
                results.append(result)
                if mismatches:
                    logger.error(
                        "Validator Service deployment drift detected",
                        extra={
                            "event": "validator_deployment_drift",
                            **result,
                        },
                    )
        except (GCPServiceImportError, GoogleAPICallError) as exc:
            logger.exception(
                "Validator Service deployment verification failed",
                extra={
                    "event": "validator_deployment_drift",
                    "provider_resource_name": resource_name,
                },
            )
            raise CommandError(str(exc)) from exc

        output = {
            "schema_version": "validibot.validator-deployment-verification.v1",
            "checked": len(results),
            "drifted": sum(not result["verified"] for result in results),
            "deployments": results,
        }
        if options["json"]:
            self.stdout.write(json.dumps(output, sort_keys=True))
        else:
            self.stdout.write(
                f"Verified {output['checked']} primary validator Service(s); "
                f"drifted={output['drifted']}."
            )
        if output["drifted"]:
            raise CommandError(
                f"{output['drifted']} validator Service deployment(s) drifted."
            )
