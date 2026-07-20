"""Import exact live Cloud Run Job facts as validator deployment routes.

The importer is intentionally provider-read-only.  It never deploys or mutates
a Job and never attaches historical attempts.  A live digest-pinned Job is
translated into the same strict, secret-free deployment contract used by the
runtime resolver, then optionally activated for future attempts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionDeploymentReadiness
from validibot.validations.constants import ExecutionDeploymentRoutingRole
from validibot.validations.constants import ExecutionProviderType
from validibot.validations.models import ValidatorExecutionDeployment
from validibot.validations.services.execution.deployments import (
    activate_execution_deployment,
)
from validibot.validations.services.execution.deployments import (
    record_execution_deployment_verification,
)

if TYPE_CHECKING:
    from validibot.validations.models import Validator

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}$")
_MEMORY_MULTIPLIERS_MIB = {
    "Ki": Decimal(1) / Decimal(1024),
    "Mi": Decimal(1),
    "Gi": Decimal(1024),
    "Ti": Decimal(1024 * 1024),
    "K": Decimal(1000) / Decimal(1024 * 1024),
    "M": Decimal(1000 * 1000) / Decimal(1024 * 1024),
    "G": Decimal(1000 * 1000 * 1000) / Decimal(1024 * 1024),
}


class GCPJobImportError(RuntimeError):
    """A live Job cannot be represented as a verified deployment route."""


@dataclass(frozen=True, slots=True)
class GCPJobObservation:
    """Bounded provider facts used to create deployment records."""

    resource_name: str
    job_name: str
    revision: str
    image_ref: str
    image_digest: str
    runtime_service_account: str
    maximum_execution_seconds: int
    maximum_cpu_millis: int
    maximum_memory_mib: int


def cloud_run_cpu_millis(raw_value: str) -> int:
    """Convert a Cloud Run CPU limit into integer millicpu."""
    value = raw_value.strip()
    try:
        if value.endswith("m"):
            result = int(value.removesuffix("m"))
        else:
            result = int(Decimal(value) * 1000)
    except (InvalidOperation, ValueError) as exc:
        raise GCPJobImportError(
            f"Unsupported Cloud Run CPU limit: {raw_value!r}"
        ) from exc
    if result < 1:
        raise GCPJobImportError("Cloud Run CPU limit must be positive.")
    return result


def cloud_run_memory_mib(raw_value: str) -> int:
    """Convert a Cloud Run binary/SI memory limit into whole mebibytes."""
    value = raw_value.strip()
    suffix = next(
        (
            candidate
            for candidate in _MEMORY_MULTIPLIERS_MIB
            if value.endswith(candidate)
        ),
        None,
    )
    if suffix is None:
        raise GCPJobImportError(f"Unsupported Cloud Run memory limit: {raw_value!r}")
    try:
        amount = Decimal(value.removesuffix(suffix))
    except InvalidOperation as exc:
        raise GCPJobImportError(
            f"Unsupported Cloud Run memory limit: {raw_value!r}"
        ) from exc
    result = int(amount * _MEMORY_MULTIPLIERS_MIB[suffix])
    if result < 1:
        raise GCPJobImportError("Cloud Run memory limit must be positive.")
    return result


def observe_cloud_run_job(job, *, expected_resource_name: str) -> GCPJobObservation:
    """Validate and normalize the exact live Cloud Run Job specification."""
    resource_name = str(getattr(job, "name", ""))
    if resource_name != expected_resource_name:
        raise GCPJobImportError(
            "Cloud Run returned a resource name different from the requested Job."
        )
    if bool(getattr(job, "reconciling", False)):
        raise GCPJobImportError(f"Cloud Run Job {resource_name} is still reconciling.")
    execution_template = getattr(job, "template", None)
    task_template = getattr(execution_template, "template", None)
    containers = getattr(task_template, "containers", None)
    if not containers or len(containers) != 1:
        raise GCPJobImportError(
            f"Cloud Run Job {resource_name} must contain exactly one container."
        )
    image_ref = str(getattr(containers[0], "image", ""))
    digest_match = _DIGEST_PATTERN.search(image_ref)
    if digest_match is None or not image_ref.endswith(f"@{digest_match.group(0)}"):
        raise GCPJobImportError(
            f"Cloud Run Job {resource_name} is not pinned to a sha256 image digest."
        )
    limits = dict(getattr(getattr(containers[0], "resources", None), "limits", {}))
    cpu = str(limits.get("cpu", ""))
    memory = str(limits.get("memory", ""))
    runtime_service_account = str(getattr(task_template, "service_account", ""))
    timeout = getattr(task_template, "timeout", None)
    timeout_seconds = int(getattr(timeout, "seconds", 0))
    labels = dict(getattr(job, "labels", {}))
    revision = str(labels.get("revision", "")).strip()
    if not revision:
        raise GCPJobImportError(
            f"Cloud Run Job {resource_name} has no immutable revision label."
        )
    if not runtime_service_account:
        raise GCPJobImportError(
            f"Cloud Run Job {resource_name} has no runtime service account."
        )
    if timeout_seconds < 1:
        raise GCPJobImportError(
            f"Cloud Run Job {resource_name} has no positive task timeout."
        )
    return GCPJobObservation(
        resource_name=resource_name,
        job_name=resource_name.rsplit("/", 1)[-1],
        revision=revision,
        image_ref=image_ref,
        image_digest=digest_match.group(0),
        runtime_service_account=runtime_service_account,
        maximum_execution_seconds=timeout_seconds,
        maximum_cpu_millis=cloud_run_cpu_millis(cpu),
        maximum_memory_mib=cloud_run_memory_mib(memory),
    )


@transaction.atomic
def register_observed_job_deployment(
    *,
    validator: Validator,
    project_id: str,
    region: str,
    observation: GCPJobObservation,
    activate_primary: bool,
) -> tuple[ValidatorExecutionDeployment, bool]:
    """Idempotently register one observed Job for one validator contract."""
    capabilities = {
        "runtime_contract_version": "validibot-execution-v1",
        "maximum_execution_seconds": observation.maximum_execution_seconds,
        "execution_shape": "JOB",
        "status_lookup": "SUPPORTED",
        "cancellation": "SUPPORTED",
        "storage_capability": "gcs_downscoped_token",
        "storage_isolation": "attempt_scoped",
        "architectures": ["linux-amd64"],
        "maximum_cpu_millis": observation.maximum_cpu_millis,
        "maximum_memory_mib": observation.maximum_memory_mib,
        "callback_authentication": "ATTEMPT_NONCE_AND_OIDC",
    }
    verification_details = {
        "observed_provider_revision": observation.revision,
        "observed_resource_name": observation.resource_name,
        "observed_image_digest": observation.image_digest,
        "checks": [
            {
                "code": "provider.resource",
                "succeeded": True,
                "summary": "Live Cloud Run Job resource identity matched.",
            },
            {
                "code": "provider.image",
                "succeeded": True,
                "summary": "Live Job image is pinned to the recorded digest.",
            },
            {
                "code": "provider.runtime_identity",
                "succeeded": True,
                "summary": "Live Job runtime service account was recorded.",
            },
        ],
    }
    defaults = {
        "display_name": f"{validator.name} Cloud Run Job {observation.revision}",
        "provider_configuration": {
            "project_id": project_id,
            "region": region,
            "job_name": observation.job_name,
            "runtime_service_account": observation.runtime_service_account,
        },
        "provider_resource_name": observation.resource_name,
        "route": "",
        "authentication_audience": "",
        "backend_release_identity": observation.revision,
        "backend_image_ref": observation.image_ref,
        "backend_image_digest": observation.image_digest,
        "expected_runtime_identity": observation.runtime_service_account,
        "declared_capabilities": capabilities,
        "verified_capabilities": capabilities,
        "readiness_state": ExecutionDeploymentReadiness.READY,
        "last_verification_succeeded": True,
        "last_verification_details": verification_details,
        "last_verified_at": timezone.now(),
        "routing_role": ExecutionDeploymentRoutingRole.INACTIVE,
        "maximum_execution_seconds": observation.maximum_execution_seconds,
        "request_timeout_seconds": None,
        "dispatch_timeout_seconds": 30,
        "minimum_instances": 0,
        "maximum_instances": None,
        "concurrency": 1,
    }
    deployment, created = ValidatorExecutionDeployment.objects.get_or_create(
        validator=validator,
        provider_type=ExecutionProviderType.GCP,
        deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_JOB,
        deployment_revision=observation.revision,
        defaults=defaults,
    )
    if not created:
        if deployment.readiness_state != ExecutionDeploymentReadiness.READY:
            raise GCPJobImportError(
                "An existing Job deployment revision is not ready and cannot be "
                "re-imported in place."
            )
        expected_values = {
            **defaults,
            "validator_id": validator.pk,
            "provider_type": ExecutionProviderType.GCP,
            "deployment_kind": ExecutionDeploymentKind.CLOUD_RUN_JOB,
            "deployment_revision": observation.revision,
        }
        immutable_mismatches = [
            field_name
            for field_name in ValidatorExecutionDeployment.IMMUTABLE_AFTER_READY_FIELDS
            if getattr(deployment, field_name) != expected_values[field_name]
        ]
        if immutable_mismatches:
            joined = ", ".join(sorted(immutable_mismatches))
            raise GCPJobImportError(
                f"Deployment {deployment.pk} conflicts with live Job fields: {joined}."
            )
        deployment.last_verification_succeeded = True
        deployment.last_verification_details = verification_details
        deployment.last_verified_at = timezone.now()
        deployment.verified_capabilities = capabilities
        deployment.save(
            update_fields=[
                "last_verification_succeeded",
                "last_verification_details",
                "last_verified_at",
                "verified_capabilities",
                "modified",
            ]
        )
    record_execution_deployment_verification(deployment, created=created)
    if activate_primary:
        deployment = activate_execution_deployment(
            deployment,
            routing_role=ExecutionDeploymentRoutingRole.PRIMARY,
        )
    return deployment, created
