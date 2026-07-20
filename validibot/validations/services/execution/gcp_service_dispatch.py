"""Dispatch one pinned validator Service attempt through its provider queue.

This is the only code allowed to turn staged attempt data into a provider task.
The task name is the attempt UUID, so application-task redelivery and Cloud
Tasks ``AlreadyExists`` converge on one provider delivery identity.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone
from validibot_shared.canonicalization import sha256_hex_for_model

from validibot.core.tasks.dispatch.http_task_client import create_http_task
from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.services.cloud_run.gcs_runtime_capabilities import (
    issue_attempt_gcs_runtime_capability,
)
from validibot.validations.services.cloud_run.launcher import (
    ProviderDispatchAmbiguousError,
)
from validibot.validations.services.cloud_run.launcher import _mark_step_run_running
from validibot.validations.services.cloud_run.launcher import (
    build_validation_storage_capability_refresh_url,
)
from validibot.validations.services.execution_attempts import (
    get_active_execution_attempt,
)
from validibot.validations.services.execution_attempts import (
    transition_execution_attempt,
)
from validibot.validations.services.execution_evidence import (
    build_input_evidence_snapshot,
)

logger = logging.getLogger(__name__)


def dispatch_cloud_run_service_validation(
    *,
    step_run,
    job_name: str,
    input_envelope_uri: str,
    execution_bundle_uri: str,
    envelope,
    submission,
    step,
    expected_image_digest: str | None = None,
) -> tuple[str, str]:
    """Create or recover the deterministic provider task for one Service attempt."""
    attempt = get_active_execution_attempt(step_run)
    if attempt is None:
        raise RuntimeError("Managed Service dispatch requires a pinned deployment.")
    deployment = attempt.deployment
    if deployment is None:
        raise RuntimeError("Managed Service dispatch requires a pinned deployment.")
    if deployment.deployment_kind != ExecutionDeploymentKind.CLOUD_RUN_SERVICE:
        raise RuntimeError("Pinned deployment is not a Cloud Run Service.")
    if deployment.provider_configuration["service_name"] != job_name:
        raise RuntimeError("Staged Service target does not match pinned deployment.")
    if expected_image_digest != deployment.backend_image_digest:
        raise RuntimeError("Staged image digest does not match pinned deployment.")
    if not getattr(settings, "GCS_VALIDATOR_ATTEMPT_CAPABILITIES_ENABLED", False):
        raise RuntimeError(
            "Validator Services require attempt-scoped GCS capability mode."
        )
    queue_name = str(getattr(settings, "GCP_VALIDATOR_TASK_QUEUE_NAME", ""))
    invoker = str(getattr(settings, "GCP_VALIDATOR_TASK_INVOKER_SERVICE_ACCOUNT", ""))
    if not queue_name or not invoker:
        raise RuntimeError("Validator provider queue and invoker must be configured.")
    if attempt.timeout_at is None:
        raise RuntimeError("Managed Service attempt has no absolute deadline.")
    if attempt.state == ExecutionAttemptState.RUNNING and attempt.provider_execution_id:
        return attempt.provider_execution_id, deployment.backend_image_digest
    if attempt.state not in {
        ExecutionAttemptState.PENDING,
        ExecutionAttemptState.DISPATCHING,
        ExecutionAttemptState.UNKNOWN,
    }:
        raise ProviderDispatchAmbiguousError(
            f"Execution attempt {attempt.pk} was already claimed"
        )
    task_id = str(attempt.pk)
    project_id = str(deployment.provider_configuration["project_id"])
    region = str(deployment.provider_configuration["region"])
    task_name = (
        f"projects/{project_id}/locations/{region}/queues/{queue_name}/tasks/{task_id}"
    )
    capability = issue_attempt_gcs_runtime_capability(
        execution_bundle_uri=execution_bundle_uri,
        project_id=project_id,
        refresh_url=build_validation_storage_capability_refresh_url(),
    )
    payload = {
        "schema_version": 1,
        "attempt_id": str(attempt.pk),
        "deployment_id": str(deployment.pk),
        "deployment_revision": deployment.deployment_revision,
        "provider_resource_name": deployment.provider_resource_name,
        "provider_task_name": task_name,
        "service_name": job_name,
        "service_revision": deployment.deployment_revision,
        "backend_image_digest": deployment.backend_image_digest,
        "input_uri": input_envelope_uri,
        "timeout_at": attempt.timeout_at.isoformat(),
        "domain_timeout_seconds": deployment.maximum_execution_seconds,
        "gcs_capability": {
            "access_token": capability.access_token,
            "expires_at": capability.expires_at.isoformat(),
            "allowed_prefix": capability.allowed_prefix,
            "project_id": capability.project_id,
            "refresh_url": capability.refresh_url,
        },
    }
    if attempt.state == ExecutionAttemptState.PENDING:
        attempt, claimed = transition_execution_attempt(
            attempt.pk,
            ExecutionAttemptState.DISPATCHING,
            provider_resource_name=deployment.provider_resource_name,
            execution_bundle_uri=execution_bundle_uri,
            input_envelope_uri=input_envelope_uri,
            input_envelope_sha256=sha256_hex_for_model(envelope),
            input_evidence_snapshot=build_input_evidence_snapshot(
                envelope,
                submission=submission,
                step=step,
            ),
            output_envelope_uri=str(envelope.context.expected_output_uri),
        )
        if not claimed:
            raise ProviderDispatchAmbiguousError(
                f"Execution attempt {attempt.pk} was already claimed"
            )
    try:
        created_task = create_http_task(
            project_id=project_id,
            region=region,
            queue_name=queue_name,
            task_id=task_id,
            endpoint_url=f"{deployment.route.rstrip('/')}/v1/execute",
            payload=payload,
            oidc_service_account=invoker,
            oidc_audience=deployment.authentication_audience,
            dispatch_deadline_seconds=int(
                settings.GCP_VALIDATOR_TASK_DISPATCH_DEADLINE_SECONDS
            ),
        )
    except Exception as exc:
        transition_execution_attempt(
            attempt.pk,
            ExecutionAttemptState.UNKNOWN,
            last_error_code="provider_task_acceptance_unknown",
            last_error="Provider task creation raised before acceptance was known.",
        )
        logger.warning(
            "Validator provider task acceptance is unknown",
            extra={
                "attempt_id": str(attempt.pk),
                "deployment_id": str(deployment.pk),
                "provider_resource_name": deployment.provider_resource_name,
                "provider_task_name": task_name,
            },
            exc_info=True,
        )
        raise ProviderDispatchAmbiguousError(
            f"Provider task acceptance is unknown for attempt {attempt.pk}"
        ) from exc
    transition_execution_attempt(
        attempt.pk,
        ExecutionAttemptState.RUNNING,
        provider_execution_id=created_task.task_name,
        provider_accepted_at=timezone.now(),
    )
    _mark_step_run_running(
        step_run,
        image_digest=deployment.backend_image_digest,
        provider_execution_id=created_task.task_name,
    )
    logger.info(
        "Validator provider task accepted",
        extra={
            "attempt_id": str(attempt.pk),
            "deployment_id": str(deployment.pk),
            "deployment_kind": deployment.deployment_kind,
            "deployment_revision": deployment.deployment_revision,
            "provider_resource_name": deployment.provider_resource_name,
            "provider_task_name": created_task.task_name,
        },
    )
    return created_task.task_name, deployment.backend_image_digest
