"""Verify and register immutable private Cloud Run validator Services."""

from __future__ import annotations

import re
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from validibot.validations.constants import CLOUD_RUN_SERVICE_DISPATCH_DEADLINE_SECONDS
from validibot.validations.constants import CLOUD_RUN_SERVICE_MAXIMUM_DOMAIN_SECONDS
from validibot.validations.constants import (
    CLOUD_RUN_SERVICE_REQUEST_TIMEOUT_LIMIT_SECONDS,
)
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionDeploymentReadiness
from validibot.validations.constants import ExecutionDeploymentRoutingRole
from validibot.validations.constants import ExecutionProviderType
from validibot.validations.models import ValidatorExecutionDeployment
from validibot.validations.services.execution.deployments import (
    activate_service_with_job_compatibility,
)
from validibot.validations.services.execution.deployments import (
    record_execution_deployment_verification,
)
from validibot.validations.services.execution.deployments import (
    update_execution_deployment_capacity,
)
from validibot.validations.services.execution.gcp_job_import import GCPJobImportError
from validibot.validations.services.execution.gcp_job_import import cloud_run_cpu_millis
from validibot.validations.services.execution.gcp_job_import import cloud_run_memory_mib

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}$")


class GCPServiceImportError(RuntimeError):
    """A live Service does not satisfy the immutable execution contract."""


@dataclass(frozen=True, slots=True)
class GCPServiceObservation:
    """Exact safe facts observed from one ready private Service revision."""

    resource_name: str
    service_name: str
    service_url: str
    revision: str
    backend_release_identity: str
    image_ref: str
    image_digest: str
    runtime_service_account: str
    invoker_service_account: str
    request_timeout_seconds: int
    maximum_cpu_millis: int
    maximum_memory_mib: int
    minimum_instances: int
    maximum_instances: int
    concurrency: int


def registered_service_observation_mismatches(
    deployment: ValidatorExecutionDeployment,
    observation: GCPServiceObservation,
) -> list[str]:
    """Return safe field names whose registered and live values differ."""
    provider_configuration = dict(deployment.provider_configuration)
    capabilities = dict(deployment.verified_capabilities)
    expected = {
        "provider_resource_name": deployment.provider_resource_name,
        "deployment_revision": deployment.deployment_revision,
        "route": deployment.route.rstrip("/"),
        "authentication_audience": deployment.authentication_audience.rstrip("/"),
        "backend_release_identity": deployment.backend_release_identity,
        "backend_image_ref": deployment.backend_image_ref,
        "backend_image_digest": deployment.backend_image_digest,
        "expected_runtime_identity": deployment.expected_runtime_identity,
        "provider_service_name": provider_configuration.get("service_name", ""),
        "provider_service_url": str(
            provider_configuration.get("service_url", "")
        ).rstrip("/"),
        "provider_runtime_identity": provider_configuration.get(
            "runtime_service_account", ""
        ),
        "provider_invoker_identity": provider_configuration.get(
            "invoker_service_account", ""
        ),
        "request_timeout_seconds": deployment.request_timeout_seconds,
        "maximum_cpu_millis": capabilities.get("maximum_cpu_millis"),
        "maximum_memory_mib": capabilities.get("maximum_memory_mib"),
        "minimum_instances": deployment.minimum_instances,
        "maximum_instances": deployment.maximum_instances,
        "concurrency": deployment.concurrency,
    }
    actual = {
        "provider_resource_name": observation.resource_name,
        "deployment_revision": observation.revision,
        "route": observation.service_url,
        "authentication_audience": observation.service_url,
        "backend_release_identity": observation.backend_release_identity,
        "backend_image_ref": observation.image_ref,
        "backend_image_digest": observation.image_digest,
        "expected_runtime_identity": observation.runtime_service_account,
        "provider_service_name": observation.service_name,
        "provider_service_url": observation.service_url,
        "provider_runtime_identity": observation.runtime_service_account,
        "provider_invoker_identity": observation.invoker_service_account,
        "request_timeout_seconds": observation.request_timeout_seconds,
        "maximum_cpu_millis": observation.maximum_cpu_millis,
        "maximum_memory_mib": observation.maximum_memory_mib,
        "minimum_instances": observation.minimum_instances,
        "maximum_instances": observation.maximum_instances,
        "concurrency": observation.concurrency,
    }
    return sorted(field for field, value in expected.items() if actual[field] != value)


def _environment(containers) -> dict[str, str]:
    """Return non-secret persistent Service environment values."""
    return {
        str(item.name): str(item.value)
        for item in getattr(containers[0], "env", [])
        if getattr(item, "value", None) is not None
    }


def _verify_invoker_policy(policy, *, invoker_service_account: str) -> None:
    """Require the dedicated identity to be the Service's only invoker."""
    expected_member = f"serviceAccount:{invoker_service_account}"
    invokers: set[str] = set()
    for binding in getattr(policy, "bindings", []):
        if str(binding.role) == "roles/run.invoker":
            invokers.update(str(member) for member in binding.members)
    if expected_member not in invokers:
        raise GCPServiceImportError(
            "Dedicated provider-task identity lacks roles/run.invoker."
        )
    if "allUsers" in invokers or "allAuthenticatedUsers" in invokers:
        raise GCPServiceImportError(
            "Validator Service permits an unauthenticated or broad invoker."
        )
    unexpected_invokers = invokers - {expected_member}
    if unexpected_invokers:
        raise GCPServiceImportError(
            "Validator Service permits an unexpected invoker identity."
        )


def observe_cloud_run_service(
    service,
    *,
    policy,
    expected_resource_name: str,
    invoker_service_account: str,
) -> GCPServiceObservation:
    """Validate live Service identity, revision, runtime, IAM, and timeouts."""
    resource_name = str(getattr(service, "name", ""))
    if resource_name != expected_resource_name:
        raise GCPServiceImportError(
            "Cloud Run returned a resource different from the requested Service."
        )
    if bool(getattr(service, "reconciling", False)):
        raise GCPServiceImportError(
            f"Cloud Run Service {resource_name} is reconciling."
        )
    latest_ready = str(getattr(service, "latest_ready_revision", ""))
    latest_created = str(getattr(service, "latest_created_revision", ""))
    if not latest_ready or latest_ready != latest_created:
        raise GCPServiceImportError(
            "Cloud Run Service latest created revision is not ready."
        )
    template = getattr(service, "template", None)
    containers = getattr(template, "containers", None)
    if not containers or len(containers) != 1:
        raise GCPServiceImportError(
            "Validator Service must have exactly one container."
        )
    image_ref = str(getattr(containers[0], "image", ""))
    digest_match = _DIGEST_PATTERN.search(image_ref)
    if digest_match is None or not image_ref.endswith(f"@{digest_match.group(0)}"):
        raise GCPServiceImportError("Validator Service image is not digest pinned.")
    limits = dict(getattr(getattr(containers[0], "resources", None), "limits", {}))
    environment = _environment(containers)
    image_digest = digest_match.group(0)
    if environment.get("VALIDIBOT_EXECUTION_SHAPE") != "service":
        raise GCPServiceImportError("Service runtime shape is not enabled.")
    if environment.get("VALIDIBOT_BACKEND_IMAGE_DIGEST") != image_digest:
        raise GCPServiceImportError("Service runtime image digest environment drifted.")
    backend_release = environment.get("VALIDIBOT_BACKEND_RELEASE", "")
    if not backend_release:
        raise GCPServiceImportError("Service backend release identity is missing.")
    resources = getattr(containers[0], "resources", None)
    if not bool(getattr(resources, "startup_cpu_boost", False)):
        raise GCPServiceImportError("Validator Service must enable Startup CPU Boost.")
    concurrency = int(getattr(template, "max_instance_request_concurrency", 0))
    timeout_seconds = int(getattr(getattr(template, "timeout", None), "seconds", 0))
    # ``gcloud run --min/--max`` configures mutable service-level capacity,
    # not immutable revision-level scaling. It can therefore be changed and
    # audited without replacing the revision pinned by in-flight attempts.
    scaling = getattr(service, "scaling", None)
    minimum_instances = int(getattr(scaling, "min_instance_count", 0))
    maximum_instances = int(getattr(scaling, "max_instance_count", 0))
    if concurrency != 1:
        raise GCPServiceImportError("Validator Service concurrency must equal one.")
    if not (
        CLOUD_RUN_SERVICE_MAXIMUM_DOMAIN_SECONDS
        < timeout_seconds
        < CLOUD_RUN_SERVICE_REQUEST_TIMEOUT_LIMIT_SECONDS
    ):
        raise GCPServiceImportError(
            "Validator Service timeout must exceed 1500 and remain below 1650."
        )
    if maximum_instances < 1 or minimum_instances > maximum_instances:
        raise GCPServiceImportError("Validator Service scaling bounds are invalid.")
    service_url = str(getattr(service, "uri", "")).rstrip("/")
    if not service_url.startswith("https://"):
        raise GCPServiceImportError("Validator Service has no canonical HTTPS URL.")
    _verify_invoker_policy(policy, invoker_service_account=invoker_service_account)
    runtime_service_account = str(getattr(template, "service_account", ""))
    if not runtime_service_account:
        raise GCPServiceImportError("Validator Service has no runtime service account.")
    try:
        cpu_millis = cloud_run_cpu_millis(str(limits.get("cpu", "")))
        memory_mib = cloud_run_memory_mib(str(limits.get("memory", "")))
    except GCPJobImportError as exc:
        raise GCPServiceImportError(str(exc)) from exc
    return GCPServiceObservation(
        resource_name=resource_name,
        service_name=resource_name.rsplit("/", 1)[-1],
        service_url=service_url,
        revision=latest_ready.rsplit("/", 1)[-1],
        backend_release_identity=backend_release,
        image_ref=image_ref,
        image_digest=image_digest,
        runtime_service_account=runtime_service_account,
        invoker_service_account=invoker_service_account,
        request_timeout_seconds=timeout_seconds,
        maximum_cpu_millis=cpu_millis,
        maximum_memory_mib=memory_mib,
        minimum_instances=minimum_instances,
        maximum_instances=maximum_instances,
        concurrency=concurrency,
    )


@transaction.atomic
def register_observed_service_deployment(
    *,
    validator,
    project_id: str,
    region: str,
    observation: GCPServiceObservation,
    maximum_execution_seconds: int,
    activate_primary: bool,
) -> tuple[ValidatorExecutionDeployment, bool]:
    """Register a verified Service route and optionally preserve Job fallback."""
    capabilities = {
        "runtime_contract_version": "validibot-execution-v1",
        "maximum_execution_seconds": maximum_execution_seconds,
        "execution_shape": "REQUEST",
        "status_lookup": "UNSUPPORTED",
        "cancellation": "BEST_EFFORT",
        "storage_capability": "gcs_downscoped_token",
        "storage_isolation": "attempt_scoped",
        "architectures": ["linux-amd64"],
        "maximum_cpu_millis": observation.maximum_cpu_millis,
        "maximum_memory_mib": observation.maximum_memory_mib,
        "callback_authentication": "ATTEMPT_NONCE_AND_OIDC",
    }
    details = {
        "observed_provider_revision": observation.revision,
        "observed_resource_name": observation.resource_name,
        "observed_image_digest": observation.image_digest,
        "checks": [
            {
                "code": "service.revision_image",
                "succeeded": True,
                "summary": "Ready revision and digest-pinned image matched.",
            },
            {
                "code": "service.private_invoker",
                "succeeded": True,
                "summary": "Dedicated task identity is the only Service invoker.",
            },
            {
                "code": "service.runtime_bounds",
                "succeeded": True,
                "summary": "Concurrency, startup CPU, resources, and timeouts matched.",
            },
        ],
    }
    defaults = {
        "display_name": f"{validator.name} Service {observation.revision}",
        "provider_configuration": {
            "project_id": project_id,
            "region": region,
            "service_name": observation.service_name,
            "service_url": observation.service_url,
            "authentication_audience": observation.service_url,
            "runtime_service_account": observation.runtime_service_account,
            "invoker_service_account": observation.invoker_service_account,
        },
        "provider_resource_name": observation.resource_name,
        "route": observation.service_url,
        "authentication_audience": observation.service_url,
        "backend_release_identity": observation.backend_release_identity,
        "backend_image_ref": observation.image_ref,
        "backend_image_digest": observation.image_digest,
        "expected_runtime_identity": observation.runtime_service_account,
        "declared_capabilities": capabilities,
        "verified_capabilities": capabilities,
        "readiness_state": ExecutionDeploymentReadiness.READY,
        "last_verification_succeeded": True,
        "last_verification_details": details,
        "last_verified_at": timezone.now(),
        "routing_role": ExecutionDeploymentRoutingRole.INACTIVE,
        "maximum_execution_seconds": maximum_execution_seconds,
        "request_timeout_seconds": observation.request_timeout_seconds,
        "dispatch_timeout_seconds": CLOUD_RUN_SERVICE_DISPATCH_DEADLINE_SECONDS,
        "minimum_instances": observation.minimum_instances,
        "maximum_instances": observation.maximum_instances,
        "concurrency": observation.concurrency,
    }
    deployment, created = ValidatorExecutionDeployment.objects.get_or_create(
        validator=validator,
        provider_type=ExecutionProviderType.GCP,
        deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
        deployment_revision=observation.revision,
        defaults=defaults,
    )
    if not created:
        if deployment.readiness_state != ExecutionDeploymentReadiness.READY:
            raise GCPServiceImportError(
                "An existing Service deployment revision is not ready and cannot "
                "be re-imported in place."
            )
        expected = {
            **defaults,
            "validator_id": validator.pk,
            "provider_type": ExecutionProviderType.GCP,
            "deployment_kind": ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
            "deployment_revision": observation.revision,
        }
        mismatches = [
            field
            for field in ValidatorExecutionDeployment.IMMUTABLE_AFTER_READY_FIELDS
            if getattr(deployment, field) != expected[field]
        ]
        if mismatches:
            raise GCPServiceImportError(
                "Registered Service conflicts with live immutable fields: "
                + ", ".join(sorted(mismatches))
            )
        deployment.last_verification_succeeded = True
        deployment.last_verification_details = details
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
        deployment = update_execution_deployment_capacity(
            deployment,
            minimum_instances=observation.minimum_instances,
            maximum_instances=observation.maximum_instances,
        )
    record_execution_deployment_verification(deployment, created=created)
    if activate_primary:
        deployment = activate_service_with_job_compatibility(deployment)
    return deployment, created


@transaction.atomic
def verify_registered_service_deployment(
    deployment: ValidatorExecutionDeployment,
    *,
    observation: GCPServiceObservation,
) -> ValidatorExecutionDeployment:
    """Re-verify one known Service and audit mutable capacity convergence."""
    selected = ValidatorExecutionDeployment.objects.select_for_update().get(
        pk=deployment.pk
    )
    if selected.readiness_state != ExecutionDeploymentReadiness.READY:
        raise GCPServiceImportError(
            "Only a ready registered Service deployment can be re-verified."
        )
    mismatches = registered_service_observation_mismatches(selected, observation)
    capacity_fields = {"minimum_instances", "maximum_instances"}
    immutable_mismatches = sorted(set(mismatches) - capacity_fields)
    if immutable_mismatches:
        raise GCPServiceImportError(
            "Registered Service drifted from immutable provider fields: "
            + ", ".join(immutable_mismatches)
        )
    selected.last_verification_succeeded = True
    selected.last_verified_at = timezone.now()
    selected.save(
        update_fields=["last_verification_succeeded", "last_verified_at", "modified"]
    )
    selected = update_execution_deployment_capacity(
        selected,
        minimum_instances=observation.minimum_instances,
        maximum_instances=observation.maximum_instances,
    )
    record_execution_deployment_verification(selected, created=False)
    return selected
