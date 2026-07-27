"""Export safe database facts used by the local validator release controller."""

import base64
import json

from django.core.management.base import BaseCommand
from django.db.models import Count

from validibot.audit.constants import AuditAction
from validibot.audit.models import AuditLogEntry
from validibot.validations.constants import EXECUTION_ATTEMPT_TERMINAL_STATES
from validibot.validations.constants import ValidatorAvailabilityState
from validibot.validations.constants import ValidatorReleaseState
from validibot.validations.models import ExecutionAttempt
from validibot.validations.models import Validator
from validibot.validations.models import ValidatorExecutionDeployment
from validibot.validations.services.execution.deployments import routing_mode_for_pair


def _time(value):
    """Return an ISO timestamp or null for stable JSON output."""
    return value.isoformat() if value is not None else None


class Command(BaseCommand):
    """Provide database input without reading the sibling backend repository."""

    help = (
        "Emit the published Validator rows, deployments, unfinished attempts, "
        "and release-routing audit facts used by the local operator script."
    )

    def add_arguments(self, parser):
        """Offer a marker form that survives Cloud Run log prefixes."""
        parser.add_argument(
            "--base64",
            action="store_true",
            help="Emit one VALIDIBOT_RELEASE_STATE_JSON base64 marker line.",
        )

    def handle(self, *args, **options):
        """Render one deterministic, credential-free JSON document."""
        validators = list(
            Validator.objects.filter(
                is_system=True,
                is_enabled=True,
                release_state=ValidatorReleaseState.PUBLISHED,
                availability_state=ValidatorAvailabilityState.AVAILABLE,
            )
            .exclude(execution_backend_slug="")
            .order_by("execution_backend_slug", "slug", "version", "pk")
        )
        deployments = list(
            ValidatorExecutionDeployment.objects.filter(
                validator_id__in=[validator.pk for validator in validators]
            )
            .select_related("validator")
            .order_by(
                "backend_slug",
                "backend_release_identity",
                "validator_id",
                "deployment_kind",
                "created",
            )
        )
        unfinished_counts = {
            str(row["deployment_id"]): row["count"]
            for row in (
                ExecutionAttempt.objects.filter(
                    deployment_id__in=[deployment.pk for deployment in deployments]
                )
                .exclude(state__in=EXECUTION_ATTEMPT_TERMINAL_STATES)
                .values("deployment_id")
                .annotate(count=Count("pk"))
            )
        }
        deployment_rows = [
            {
                "deployment_id": str(deployment.pk),
                "validator_id": str(deployment.validator_id),
                "backend": deployment.backend_slug,
                "version": deployment.backend_release_identity,
                "source_release_tag": deployment.source_release_tag,
                "release_record_sha256": deployment.release_record_sha256,
                "kind": deployment.deployment_kind,
                "provider_type": deployment.provider_type,
                "provider_resource_name": deployment.provider_resource_name,
                "provider_configuration": deployment.provider_configuration,
                "provider_spec_sha256": deployment.provider_spec_sha256,
                "execution_config_sha256": deployment.execution_config_sha256,
                "image_ref": deployment.backend_image_ref,
                "image_digest": deployment.backend_image_digest,
                "runtime_identity": deployment.expected_runtime_identity,
                "readiness": deployment.readiness_state,
                "routing_role": deployment.routing_role,
                "blocked": deployment.emergency_blocked,
                "accepted_at": _time(deployment.accepted_at),
                "deactivated_at": _time(deployment.deactivated_at),
                "deactivation_cause": deployment.deactivation_cause,
                "provider_deleted_at": _time(deployment.provider_deleted_at),
                "retired_at": _time(deployment.retired_at),
                "retirement_reason": deployment.retirement_reason,
                "minimum_instances": deployment.minimum_instances,
                "maximum_instances": deployment.maximum_instances,
                "last_verified_at": _time(deployment.last_verified_at),
                "last_verification_succeeded": (deployment.last_verification_succeeded),
                "unfinished_attempts": unfinished_counts.get(
                    str(deployment.pk),
                    0,
                ),
            }
            for deployment in deployments
        ]
        validator_rows = []
        for validator in validators:
            routes = [
                deployment
                for deployment in deployments
                if deployment.validator_id == validator.pk
            ]
            active_job = next(
                (
                    item
                    for item in routes
                    if item.deployment_kind == "CLOUD_RUN_JOB"
                    and item.routing_role in {"PRIMARY", "LONG_RUNNING"}
                ),
                None,
            )
            active_service = next(
                (
                    item
                    for item in routes
                    if item.deployment_kind == "CLOUD_RUN_SERVICE"
                    and (
                        item.routing_role in {"PRIMARY", "LONG_RUNNING"}
                        or (
                            active_job is not None
                            and item.backend_slug == active_job.backend_slug
                            and item.backend_release_identity
                            == active_job.backend_release_identity
                            and item.backend_image_digest
                            == active_job.backend_image_digest
                            and item.release_record_sha256
                            == active_job.release_record_sha256
                        )
                    )
                ),
                None,
            )
            mode = (
                routing_mode_for_pair(service=active_service, job=active_job).value
                if active_service is not None and active_job is not None
                else "inactive"
            )
            validator_rows.append(
                {
                    "validator_id": str(validator.pk),
                    "slug": validator.slug,
                    "version": validator.version,
                    "backend": validator.execution_backend_slug,
                    "runtime_contract": validator.execution_runtime_contract,
                    "routing_mode": mode,
                    "active_service_deployment_id": (
                        str(active_service.pk) if active_service else None
                    ),
                    "active_job_deployment_id": (
                        str(active_job.pk) if active_job else None
                    ),
                }
            )
        rollback_events = list(
            AuditLogEntry.objects.filter(
                action=AuditAction.VALIDATOR_DEPLOYMENT_DEACTIVATED,
                metadata__routing_mode="normal",
            )
            .order_by("occurred_at", "pk")
            .values("occurred_at", "target_id", "changes", "metadata")
        )
        output = {
            "schema_version": "validibot.validator-release-state.v1",
            "validators": validator_rows,
            "deployments": deployment_rows,
            "routing_events": [
                {
                    "occurred_at": _time(event["occurred_at"]),
                    "deployment_id": event["target_id"],
                    "changes": event["changes"],
                    "metadata": event["metadata"],
                }
                for event in rollback_events
            ],
        }
        serialized = json.dumps(output, sort_keys=True, separators=(",", ":"))
        if options["base64"]:
            encoded = base64.b64encode(serialized.encode()).decode()
            self.stdout.write(f"VALIDIBOT_RELEASE_STATE_JSON={encoded}")
            return
        self.stdout.write(json.dumps(output, indent=2, sort_keys=True))
