"""Resolve, snapshot, and activate managed validator execution deployments.

Resolution is deliberately small and fail-closed.  It runs while the logical
step row is locked by attempt allocation, selects only an explicitly activated
and verified route for the exact Validator row, and never treats provider
failure as permission to choose another deployment after contact.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from validibot.audit.constants import AuditAction
from validibot.audit.services import ActorSpec
from validibot.audit.services import AuditLogService
from validibot.validations.constants import CallbackAuthenticationMethod
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionDeploymentReadiness
from validibot.validations.constants import ExecutionDeploymentRoutingRole
from validibot.validations.constants import ExecutionProviderType
from validibot.validations.constants import RuntimeStorageIsolation
from validibot.validations.constants import StorageCapabilityMode
from validibot.validations.constants import ValidatorExecutionProfile
from validibot.validations.services.execution.deployment_schemas import (
    DeploymentRouteSnapshot,
)
from validibot.validations.services.execution.deployment_schemas import (
    parse_deployment_capabilities,
)

if TYPE_CHECKING:
    from validibot.validations.models import Validator
    from validibot.validations.models import ValidatorExecutionDeployment


class ExecutionDeploymentResolutionError(RuntimeError):
    """No explicitly activated deployment can safely execute the attempt."""


def _record_operator_audit(
    deployment: ValidatorExecutionDeployment,
    *,
    action: AuditAction,
    changes: dict[str, object],
    metadata: dict[str, object] | None = None,
) -> None:
    """Record a secret-free system actor event for an operator route change."""
    AuditLogService.record(
        action=action,
        actor=ActorSpec(email="validibot-operator@system.local"),
        target=deployment,
        changes=changes,
        metadata={
            "validator_id": str(deployment.validator_id),
            "deployment_kind": deployment.deployment_kind,
            "deployment_revision": deployment.deployment_revision,
            "provider_resource_name": deployment.provider_resource_name,
            **(metadata or {}),
        },
    )


def _record_displaced_route_audits(
    deployments: list[ValidatorExecutionDeployment],
    *,
    replacement: ValidatorExecutionDeployment,
    modified_at,
) -> None:
    """Record why every route displaced by an activation became inactive."""
    for deployment in deployments:
        previous_role = deployment.routing_role
        deployment.routing_role = ExecutionDeploymentRoutingRole.INACTIVE
        deployment.activated_at = None
        deployment.modified = modified_at
        _record_operator_audit(
            deployment,
            action=AuditAction.VALIDATOR_DEPLOYMENT_DEACTIVATED,
            changes={
                "routing_role": [
                    previous_role,
                    ExecutionDeploymentRoutingRole.INACTIVE,
                ]
            },
            metadata={
                "replacement_deployment_id": str(replacement.pk),
                "replacement_routing_role": replacement.routing_role,
            },
        )


def record_execution_deployment_verification(
    deployment: ValidatorExecutionDeployment,
    *,
    created: bool,
) -> None:
    """Audit one explicit operator import/readiness verification."""
    _record_operator_audit(
        deployment,
        action=(
            AuditAction.VALIDATOR_DEPLOYMENT_REGISTERED
            if created
            else AuditAction.VALIDATOR_DEPLOYMENT_VERIFIED
        ),
        changes={},
        metadata={
            "readiness_state": deployment.readiness_state,
            "verification_succeeded": deployment.last_verification_succeeded,
            "verified_at": (
                deployment.last_verified_at.isoformat()
                if deployment.last_verified_at
                else None
            ),
        },
    )


@transaction.atomic
def update_execution_deployment_capacity(
    deployment: ValidatorExecutionDeployment,
    *,
    minimum_instances: int,
    maximum_instances: int,
) -> ValidatorExecutionDeployment:
    """Record verified Service-level scaling without changing revision identity."""
    from validibot.validations.models import ValidatorExecutionDeployment

    if minimum_instances < 0 or maximum_instances < 1:
        raise ValueError("Service capacity requires minimum >= 0 and maximum >= 1.")
    if minimum_instances > maximum_instances:
        raise ValueError("Service minimum instances cannot exceed maximum instances.")
    selected = ValidatorExecutionDeployment.objects.select_for_update().get(
        pk=deployment.pk
    )
    if selected.deployment_kind != ExecutionDeploymentKind.CLOUD_RUN_SERVICE:
        raise ValueError("Only Cloud Run Service deployments expose instance capacity.")
    if selected.readiness_state != ExecutionDeploymentReadiness.READY:
        raise ValueError("Only ready Cloud Run Service capacity may be updated.")
    previous = (selected.minimum_instances, selected.maximum_instances)
    current = (minimum_instances, maximum_instances)
    if previous == current:
        return selected
    selected.minimum_instances = minimum_instances
    selected.maximum_instances = maximum_instances
    selected.save(update_fields=["minimum_instances", "maximum_instances", "modified"])
    _record_operator_audit(
        selected,
        action=AuditAction.VALIDATOR_DEPLOYMENT_CAPACITY_UPDATED,
        changes={
            "minimum_instances": [previous[0], minimum_instances],
            "maximum_instances": [previous[1], maximum_instances],
        },
    )
    return selected


def ensure_execution_deployment_can_retire(
    deployment: ValidatorExecutionDeployment,
) -> None:
    """Fail unless an inactive, cold Service has no nonterminal attempts."""
    from validibot.validations.constants import EXECUTION_ATTEMPT_TERMINAL_STATES
    from validibot.validations.models import ExecutionAttempt

    if deployment.deployment_kind != ExecutionDeploymentKind.CLOUD_RUN_SERVICE:
        raise ValueError("Only Cloud Run Service deployments can use this cleanup.")
    if deployment.routing_role != ExecutionDeploymentRoutingRole.INACTIVE:
        raise ExecutionDeploymentResolutionError(
            f"Deployment {deployment.pk} still occupies a routing slot."
        )
    if deployment.minimum_instances != 0:
        raise ExecutionDeploymentResolutionError(
            f"Deployment {deployment.pk} must have minimum instances zero."
        )
    if (
        ExecutionAttempt.objects.filter(deployment=deployment)
        .exclude(state__in=EXECUTION_ATTEMPT_TERMINAL_STATES)
        .exists()
    ):
        raise ExecutionDeploymentResolutionError(
            f"Deployment {deployment.pk} still has a nonterminal attempt."
        )


@transaction.atomic
def retire_execution_deployment(
    deployment: ValidatorExecutionDeployment,
) -> ValidatorExecutionDeployment:
    """Retire an inactive Service after its provider resource was deleted."""
    from validibot.validations.models import ValidatorExecutionDeployment

    selected = ValidatorExecutionDeployment.objects.select_for_update().get(
        pk=deployment.pk
    )
    ensure_execution_deployment_can_retire(selected)
    if selected.readiness_state == ExecutionDeploymentReadiness.RETIRED:
        return selected
    previous_state = selected.readiness_state
    selected.readiness_state = ExecutionDeploymentReadiness.RETIRED
    selected.save(update_fields=["readiness_state", "modified"])
    _record_operator_audit(
        selected,
        action=AuditAction.VALIDATOR_DEPLOYMENT_RETIRED,
        changes={"readiness_state": [previous_state, selected.readiness_state]},
        metadata={"provider_resource_deleted": True},
    )
    return selected


def effective_execution_profile(*, step) -> ValidatorExecutionProfile:
    """Return the validated workload profile requested by a workflow step."""
    raw_value = (getattr(step, "config", None) or {}).get(
        "execution_profile",
        ValidatorExecutionProfile.FAST_RESPONSE,
    )
    try:
        return ValidatorExecutionProfile(raw_value)
    except ValueError as exc:
        allowed = ", ".join(ValidatorExecutionProfile.values)
        raise ExecutionDeploymentResolutionError(
            f"execution_profile must be one of: {allowed}."
        ) from exc


def effective_execution_budget_seconds(*, step) -> int:
    """Return the operator-bounded domain budget for one authored profile.

    Fast-response steps use the Service-eligible default. Long-running steps
    receive the site-wide validator ceiling without asking a solo operator or
    workflow author to coordinate a second timeout field. Machine-authored
    workflow imports may still request a narrower explicit timeout.
    """
    from django.conf import settings

    profile = effective_execution_profile(step=step)
    configured = (getattr(step, "config", None) or {}).get("execution_timeout_seconds")
    value = (
        configured
        if configured is not None
        else (
            getattr(settings, "VALIDATOR_TIMEOUT_SECONDS", 3600)
            if profile == ValidatorExecutionProfile.LONG_RUNNING
            else getattr(settings, "VALIDATOR_DEFAULT_EXECUTION_SECONDS", 1500)
        )
    )
    if isinstance(value, bool):
        raise ExecutionDeploymentResolutionError(
            "execution_timeout_seconds must be a positive integer."
        )
    try:
        budget = int(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionDeploymentResolutionError(
            "execution_timeout_seconds must be a positive integer."
        ) from exc
    maximum = int(getattr(settings, "VALIDATOR_TIMEOUT_SECONDS", 3600))
    if budget < 1 or budget > maximum:
        raise ExecutionDeploymentResolutionError(
            f"execution_timeout_seconds must be between 1 and {maximum}."
        )
    return budget


def _validated_capabilities(deployment: ValidatorExecutionDeployment):
    """Return verified capabilities after enforcing baseline runtime needs."""
    if not deployment.verified_capabilities:
        raise ExecutionDeploymentResolutionError(
            f"Deployment {deployment.pk} has no verified capabilities."
        )
    capabilities = parse_deployment_capabilities(
        deployment_kind=deployment.deployment_kind,
        capabilities=deployment.verified_capabilities,
    )
    unsupported: list[str] = []
    if capabilities.runtime_contract_version != "validibot-execution-v1":
        unsupported.append("runtime contract validibot-execution-v1")
    if capabilities.storage_capability != StorageCapabilityMode.GCS_DOWNSCOPED_TOKEN:
        unsupported.append("downscoped GCS storage")
    if capabilities.storage_isolation != RuntimeStorageIsolation.ATTEMPT_SCOPED:
        unsupported.append("attempt-scoped storage isolation")
    if (
        capabilities.callback_authentication
        != CallbackAuthenticationMethod.ATTEMPT_NONCE_AND_OIDC
    ):
        unsupported.append("attempt nonce plus OIDC callback authentication")
    if "linux-amd64" not in capabilities.architectures:
        unsupported.append("linux-amd64")
    if unsupported:
        joined = ", ".join(unsupported)
        raise ExecutionDeploymentResolutionError(
            f"Deployment {deployment.pk} lacks required capabilities: {joined}."
        )
    return capabilities


def resolve_execution_deployment(
    *,
    validator: Validator,
    effective_budget_seconds: int,
    execution_profile: ValidatorExecutionProfile | str = (
        ValidatorExecutionProfile.FAST_RESPONSE
    ),
    for_update: bool = False,
) -> ValidatorExecutionDeployment:
    """Select the exact active route for a managed attempt before dispatch.

    The workflow's profile makes route selection explicit before provider
    contact. Fast-response work uses the primary route. Long-running work uses
    the compatibility route, or a primary Job while an operator rollback is in
    effect. Missing, blocked, unready, drifted, or capability-incompatible
    deployments fail closed; runtime failure never authorizes route switching.
    """
    from validibot.validations.models import ValidatorExecutionDeployment

    if effective_budget_seconds < 1:
        raise ExecutionDeploymentResolutionError(
            "The effective execution budget must be at least one second."
        )
    try:
        profile = ValidatorExecutionProfile(execution_profile)
    except ValueError as exc:
        raise ExecutionDeploymentResolutionError(
            f"Unknown validator execution profile: {execution_profile!r}."
        ) from exc
    queryset = ValidatorExecutionDeployment.objects.filter(validator=validator)
    if for_update:
        queryset = queryset.select_for_update()
    deployments = {
        deployment.routing_role: deployment
        for deployment in queryset.filter(
            routing_role__in=(
                ExecutionDeploymentRoutingRole.PRIMARY,
                ExecutionDeploymentRoutingRole.LONG_RUNNING,
            )
        )
    }
    primary = deployments.get(ExecutionDeploymentRoutingRole.PRIMARY)
    compatibility = deployments.get(ExecutionDeploymentRoutingRole.LONG_RUNNING)

    if profile == ValidatorExecutionProfile.LONG_RUNNING:
        selected = compatibility
        route_label = "Long-running"
        if (
            selected is None
            and primary is not None
            and primary.deployment_kind == ExecutionDeploymentKind.CLOUD_RUN_JOB
        ):
            # Operator rollback makes the retained Job primary and clears the
            # compatibility slot. It remains the truthful long-running route.
            selected = primary
            route_label = "Primary Job"
    else:
        selected = primary
        route_label = "Primary"

    if selected is None:
        missing_role = (
            "long-running"
            if profile == ValidatorExecutionProfile.LONG_RUNNING
            else "primary"
        )
        raise ExecutionDeploymentResolutionError(
            f"Validator {validator.pk} has no activated {missing_role} deployment."
        )
    if selected.readiness_state != ExecutionDeploymentReadiness.READY:
        raise ExecutionDeploymentResolutionError(
            f"{route_label} deployment {selected.pk} is not ready."
        )
    if selected.emergency_blocked:
        raise ExecutionDeploymentResolutionError(
            f"{route_label} deployment {selected.pk} is emergency blocked."
        )
    capabilities = _validated_capabilities(selected)
    if effective_budget_seconds > capabilities.maximum_execution_seconds:
        guidance = (
            " Choose the Long-running profile for larger work."
            if profile == ValidatorExecutionProfile.FAST_RESPONSE
            and selected.deployment_kind == ExecutionDeploymentKind.CLOUD_RUN_SERVICE
            else ""
        )
        raise ExecutionDeploymentResolutionError(
            f"The {effective_budget_seconds}-second attempt budget exceeds "
            f"{route_label.lower()} deployment {selected.pk}'s verified maximum."
            f"{guidance}"
        )
    return selected


def build_deployment_snapshot(
    deployment: ValidatorExecutionDeployment,
) -> dict[str, object]:
    """Return the typed JSON-safe evidence snapshot for one selected route."""
    snapshot = DeploymentRouteSnapshot(
        deployment_id=deployment.pk,
        validator_id=deployment.validator_id,
        selected_at=timezone.now(),
        provider_type=ExecutionProviderType(deployment.provider_type),
        deployment_kind=ExecutionDeploymentKind(deployment.deployment_kind),
        deployment_revision=deployment.deployment_revision,
        provider_resource_name=deployment.provider_resource_name,
        route=deployment.route,
        authentication_audience=deployment.authentication_audience,
        backend_release_identity=deployment.backend_release_identity,
        backend_image_ref=deployment.backend_image_ref,
        backend_image_digest=deployment.backend_image_digest,
        expected_runtime_identity=deployment.expected_runtime_identity,
        routing_role=ExecutionDeploymentRoutingRole(deployment.routing_role),
        declared_capabilities=deployment.declared_capabilities,
        verified_capabilities=deployment.verified_capabilities,
        maximum_execution_seconds=deployment.maximum_execution_seconds,
        request_timeout_seconds=deployment.request_timeout_seconds,
        dispatch_timeout_seconds=deployment.dispatch_timeout_seconds,
        minimum_instances=deployment.minimum_instances,
        maximum_instances=deployment.maximum_instances,
        concurrency=deployment.concurrency,
    )
    return snapshot.model_dump(mode="json")


@transaction.atomic
def activate_execution_deployment(
    deployment: ValidatorExecutionDeployment,
    *,
    routing_role: ExecutionDeploymentRoutingRole,
) -> ValidatorExecutionDeployment:
    """Transactionally move one READY deployment into an active routing slot."""
    from validibot.validations.models import ValidatorExecutionDeployment

    if routing_role == ExecutionDeploymentRoutingRole.INACTIVE:
        raise ValueError("Use an explicit block or rollback workflow to deactivate.")
    selected = ValidatorExecutionDeployment.objects.select_for_update().get(
        pk=deployment.pk
    )
    previous_role = selected.routing_role
    if selected.readiness_state != ExecutionDeploymentReadiness.READY:
        raise ExecutionDeploymentResolutionError(
            f"Deployment {selected.pk} is not ready for activation."
        )
    if selected.emergency_blocked:
        raise ExecutionDeploymentResolutionError(
            f"Deployment {selected.pk} is emergency blocked."
        )
    _validated_capabilities(selected)
    displaced = list(
        ValidatorExecutionDeployment.objects.select_for_update()
        .filter(
            validator_id=selected.validator_id,
            routing_role=routing_role,
        )
        .exclude(pk=selected.pk)
    )
    now = timezone.now()
    ValidatorExecutionDeployment.objects.filter(
        pk__in=[item.pk for item in displaced]
    ).update(
        routing_role=ExecutionDeploymentRoutingRole.INACTIVE,
        activated_at=None,
        modified=now,
    )
    selected.routing_role = routing_role
    selected.activated_at = now
    selected.save(update_fields=["routing_role", "activated_at", "modified"])
    _record_displaced_route_audits(
        displaced,
        replacement=selected,
        modified_at=now,
    )
    if previous_role != routing_role:
        _record_operator_audit(
            selected,
            action=AuditAction.VALIDATOR_DEPLOYMENT_ACTIVATED,
            changes={"routing_role": [previous_role, routing_role]},
        )
    return selected


@transaction.atomic
def activate_service_with_job_compatibility(
    deployment: ValidatorExecutionDeployment,
) -> ValidatorExecutionDeployment:
    """Make a Service primary while retaining the current Job for long work."""
    from validibot.validations.models import ValidatorExecutionDeployment

    routes = list(
        ValidatorExecutionDeployment.objects.select_for_update().filter(
            validator_id=deployment.validator_id
        )
    )
    selected = next((item for item in routes if item.pk == deployment.pk), None)
    if selected is None:
        raise ExecutionDeploymentResolutionError("Service deployment was not found.")
    if selected.deployment_kind != ExecutionDeploymentKind.CLOUD_RUN_SERVICE:
        raise ExecutionDeploymentResolutionError(
            "Service activation requires a Cloud Run Service deployment."
        )
    if (
        selected.readiness_state != ExecutionDeploymentReadiness.READY
        or selected.emergency_blocked
    ):
        raise ExecutionDeploymentResolutionError(
            f"Service deployment {selected.pk} is not eligible for activation."
        )
    _validated_capabilities(selected)
    compatibility = next(
        (
            item
            for item in routes
            if item.deployment_kind == ExecutionDeploymentKind.CLOUD_RUN_JOB
            and item.routing_role
            == (
                ExecutionDeploymentRoutingRole.LONG_RUNNING
                if selected.routing_role == ExecutionDeploymentRoutingRole.PRIMARY
                else ExecutionDeploymentRoutingRole.PRIMARY
            )
            and item.readiness_state == ExecutionDeploymentReadiness.READY
            and not item.emergency_blocked
        ),
        None,
    )
    if compatibility is None:
        raise ExecutionDeploymentResolutionError(
            "A ready primary Cloud Run Job is required before Service activation."
        )
    selected_previous_role = selected.routing_role
    compatibility_previous_role = compatibility.routing_role
    now = timezone.now()
    displaced = [
        item
        for item in routes
        if item.pk not in {selected.pk, compatibility.pk}
        and item.routing_role
        in {
            ExecutionDeploymentRoutingRole.PRIMARY,
            ExecutionDeploymentRoutingRole.LONG_RUNNING,
        }
    ]
    ValidatorExecutionDeployment.objects.filter(
        validator_id=selected.validator_id,
        routing_role=ExecutionDeploymentRoutingRole.LONG_RUNNING,
    ).exclude(pk=compatibility.pk).update(
        routing_role=ExecutionDeploymentRoutingRole.INACTIVE,
        activated_at=None,
        modified=now,
    )
    ValidatorExecutionDeployment.objects.filter(
        validator_id=selected.validator_id,
        routing_role=ExecutionDeploymentRoutingRole.PRIMARY,
    ).exclude(pk__in=(selected.pk, compatibility.pk)).update(
        routing_role=ExecutionDeploymentRoutingRole.INACTIVE,
        activated_at=None,
        modified=now,
    )
    compatibility.routing_role = ExecutionDeploymentRoutingRole.LONG_RUNNING
    compatibility.activated_at = now
    compatibility.save(update_fields=["routing_role", "activated_at", "modified"])
    selected.routing_role = ExecutionDeploymentRoutingRole.PRIMARY
    selected.activated_at = now
    selected.save(update_fields=["routing_role", "activated_at", "modified"])
    _record_displaced_route_audits(
        displaced,
        replacement=selected,
        modified_at=now,
    )
    if (
        selected_previous_role != selected.routing_role
        or compatibility_previous_role != compatibility.routing_role
    ):
        _record_operator_audit(
            selected,
            action=AuditAction.VALIDATOR_DEPLOYMENT_ACTIVATED,
            changes={"routing_role": [selected_previous_role, selected.routing_role]},
            metadata={
                "long_running_deployment_id": str(compatibility.pk),
                "long_running_previous_role": compatibility_previous_role,
                "long_running_role": compatibility.routing_role,
            },
        )
    return selected


@transaction.atomic
def set_execution_deployment_block(
    deployment: ValidatorExecutionDeployment,
    *,
    blocked: bool,
    reason: str = "",
) -> ValidatorExecutionDeployment:
    """Set or clear a route block through one audited, locked operation."""
    from validibot.validations.models import ValidatorExecutionDeployment

    selected = ValidatorExecutionDeployment.objects.select_for_update().get(
        pk=deployment.pk
    )
    normalized_reason = reason.strip()
    if blocked and not normalized_reason:
        raise ValueError("Blocking a deployment requires an operator reason.")
    if selected.emergency_blocked == blocked and selected.emergency_block_reason == (
        normalized_reason if blocked else ""
    ):
        return selected
    previous_blocked = selected.emergency_blocked
    previous_reason = selected.emergency_block_reason
    selected.emergency_blocked = blocked
    selected.emergency_block_reason = normalized_reason if blocked else ""
    selected.save(
        update_fields=[
            "emergency_blocked",
            "emergency_block_reason",
            "modified",
        ]
    )
    _record_operator_audit(
        selected,
        action=(
            AuditAction.VALIDATOR_DEPLOYMENT_BLOCKED
            if blocked
            else AuditAction.VALIDATOR_DEPLOYMENT_UNBLOCKED
        ),
        changes={
            "emergency_blocked": [previous_blocked, blocked],
            "emergency_block_reason": [
                previous_reason,
                selected.emergency_block_reason,
            ],
        },
    )
    return selected
