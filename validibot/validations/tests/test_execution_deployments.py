"""Tests for durable validator execution deployment identity.

These tests cover the provider-neutral record introduced before any traffic is
moved from Cloud Run Jobs to Services.  The record must preserve exact image
and provider provenance, validate its JSON contracts, and prevent two routes
from occupying the same validator routing slot.
"""

from copy import deepcopy

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.utils import timezone

from validibot.audit.constants import AuditAction
from validibot.audit.models import AuditLogEntry
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionDeploymentReadiness
from validibot.validations.constants import ExecutionDeploymentRoutingRole
from validibot.validations.constants import ExecutionProviderType
from validibot.validations.models import ValidatorExecutionDeployment
from validibot.validations.services.execution.deployments import (
    ExecutionDeploymentResolutionError,
)
from validibot.validations.services.execution.deployments import (
    ensure_execution_deployment_can_retire,
)
from validibot.validations.services.execution.deployments import (
    resolve_execution_deployment,
)
from validibot.validations.services.execution.deployments import (
    retire_execution_deployment,
)
from validibot.validations.services.execution.deployments import (
    set_execution_deployment_block,
)
from validibot.validations.services.execution.deployments import (
    update_execution_deployment_capacity,
)
from validibot.validations.services.execution_attempts import (
    get_or_create_execution_attempt,
)
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory

DIGEST = "sha256:" + "a" * 64
PROJECT_ID = "validibot-prod"
REGION = "australia-southeast1"
RUNTIME_IDENTITY = "validator-runtime@validibot-prod.iam.gserviceaccount.com"
EXPECTED_SHARED_DEPLOYMENT_COUNT = 2
ATTEMPT_BUDGET_SECONDS = 900
DEADLINE_TOLERANCE_SECONDS = 1
JOB_FINALIZATION_MARGIN_SECONDS = 120


def _job_configuration(job_name="validibot-energyplus"):
    """Return the secret-free provider coordinates for one Cloud Run Job."""
    return {
        "project_id": PROJECT_ID,
        "region": REGION,
        "job_name": job_name,
        "runtime_service_account": RUNTIME_IDENTITY,
    }


def _job_capabilities():
    """Return the initial capability contract for the retained Job route."""
    return {
        "runtime_contract_version": "validibot-execution-v1",
        "maximum_execution_seconds": 1500,
        "execution_shape": "JOB",
        "status_lookup": "SUPPORTED",
        "cancellation": "SUPPORTED",
        "storage_capability": "gcs_downscoped_token",
        "storage_isolation": "attempt_scoped",
        "architectures": ["linux-amd64"],
        "maximum_cpu_millis": 4000,
        "maximum_memory_mib": 8192,
        "callback_authentication": "ATTEMPT_NONCE_AND_OIDC",
    }


def _job_deployment(*, validator, revision="v0.14.0", job_name=None, **overrides):
    """Build a valid unsaved Job deployment with one overridable contract."""
    job_name = job_name or f"validibot-{validator.slug}-{revision.replace('.', '-')}"
    configuration = _job_configuration(job_name)
    values = {
        "validator": validator,
        "provider_type": ExecutionProviderType.GCP,
        "deployment_kind": ExecutionDeploymentKind.CLOUD_RUN_JOB,
        "display_name": f"{validator.name} Job {revision}",
        "deployment_revision": revision,
        "provider_configuration": configuration,
        "provider_resource_name": (
            f"projects/{PROJECT_ID}/locations/{REGION}/jobs/{job_name}"
        ),
        "backend_release_identity": revision,
        "backend_image_ref": f"{REGION}-docker.pkg.dev/{PROJECT_ID}/validibot/"
        f"backend@{DIGEST}",
        "backend_image_digest": DIGEST,
        "expected_runtime_identity": RUNTIME_IDENTITY,
        "declared_capabilities": _job_capabilities(),
        "maximum_execution_seconds": 1500,
        "dispatch_timeout_seconds": 30,
        "minimum_instances": 0,
        "maximum_instances": 10,
        "concurrency": 1,
    }
    values.update(overrides)
    if (
        values.get("readiness_state") == ExecutionDeploymentReadiness.READY
        and "last_verification_details" not in overrides
    ):
        values["last_verification_details"] = {
            "observed_provider_revision": values["deployment_revision"],
            "observed_resource_name": values["provider_resource_name"],
            "observed_image_digest": values["backend_image_digest"],
            "checks": [
                {
                    "code": "provider.resource",
                    "succeeded": True,
                    "summary": "Provider identity matched.",
                }
            ],
        }
    return ValidatorExecutionDeployment(**values)


def _service_deployment(*, validator, revision="service-r1", **overrides):
    """Build a valid unsaved private Service deployment contract."""
    service_name = f"validibot-{validator.pk}-service"
    service_url = f"https://{service_name}-{REGION}.a.run.app"
    capabilities = _job_capabilities()
    capabilities.update(
        {
            "execution_shape": "REQUEST",
            "status_lookup": "UNSUPPORTED",
        }
    )
    values = {
        "validator": validator,
        "provider_type": ExecutionProviderType.GCP,
        "deployment_kind": ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
        "display_name": f"{validator.name} Service {revision}",
        "deployment_revision": revision,
        "provider_configuration": {
            "project_id": PROJECT_ID,
            "region": REGION,
            "service_name": service_name,
            "service_url": service_url,
            "authentication_audience": service_url,
            "runtime_service_account": RUNTIME_IDENTITY,
            "invoker_service_account": (
                "validator-invoker@validibot-prod.iam.gserviceaccount.com"
            ),
        },
        "provider_resource_name": (
            f"projects/{PROJECT_ID}/locations/{REGION}/services/{service_name}"
        ),
        "route": service_url,
        "authentication_audience": service_url,
        "backend_release_identity": revision,
        "backend_image_ref": (
            f"{REGION}-docker.pkg.dev/{PROJECT_ID}/validibot/backend@{DIGEST}"
        ),
        "backend_image_digest": DIGEST,
        "expected_runtime_identity": RUNTIME_IDENTITY,
        "declared_capabilities": capabilities,
        "maximum_execution_seconds": 1500,
        "request_timeout_seconds": 1649,
        "dispatch_timeout_seconds": 1800,
        "minimum_instances": 0,
        "maximum_instances": 10,
        "concurrency": 1,
    }
    values.update(overrides)
    if (
        values.get("readiness_state") == ExecutionDeploymentReadiness.READY
        and "last_verification_details" not in overrides
    ):
        values["last_verification_details"] = {
            "observed_provider_revision": values["deployment_revision"],
            "observed_resource_name": values["provider_resource_name"],
            "observed_image_digest": values["backend_image_digest"],
            "checks": [
                {
                    "code": "provider.resource",
                    "succeeded": True,
                    "summary": "Provider identity matched.",
                }
            ],
        }
    return ValidatorExecutionDeployment(**values)


def _save_ready(deployment, *, role):
    """Persist a deployment with complete successful readiness evidence."""
    deployment.readiness_state = ExecutionDeploymentReadiness.READY
    deployment.routing_role = role
    deployment.verified_capabilities = deepcopy(deployment.declared_capabilities)
    deployment.last_verification_succeeded = True
    deployment.last_verified_at = timezone.now()
    deployment.last_verification_details = {
        "observed_provider_revision": deployment.deployment_revision,
        "observed_resource_name": deployment.provider_resource_name,
        "observed_image_digest": deployment.backend_image_digest,
        "checks": [
            {
                "code": "provider.resource",
                "succeeded": True,
                "summary": "Provider identity matched.",
            }
        ],
    }
    deployment.save()
    return deployment


@pytest.mark.django_db
def test_job_deployment_accepts_exact_provider_and_image_identity():
    """A complete digest-pinned Job route is valid before runtime behavior changes."""
    deployment = _job_deployment(validator=ValidatorFactory())

    deployment.full_clean()

    assert deployment.provider_resource_name.endswith(
        f"/jobs/{deployment.provider_configuration['job_name']}"
    )


@pytest.mark.django_db
def test_deployment_rejects_resource_name_not_derived_from_configuration():
    """Provider JSON and the indexed canonical resource cannot drift apart."""
    deployment = _job_deployment(
        validator=ValidatorFactory(),
        provider_resource_name="projects/other/locations/elsewhere/jobs/drifted",
    )

    with pytest.raises(ValidationError) as exc_info:
        deployment.full_clean()

    assert "provider_resource_name" in exc_info.value.message_dict


@pytest.mark.django_db
def test_deployment_rejects_image_reference_not_pinned_to_its_digest():
    """A floating or mismatched image cannot become durable route provenance."""
    deployment = _job_deployment(
        validator=ValidatorFactory(),
        backend_image_ref=(
            f"{REGION}-docker.pkg.dev/{PROJECT_ID}/validibot/backend:latest"
        ),
    )

    with pytest.raises(ValidationError) as exc_info:
        deployment.full_clean()

    assert "backend_image_ref" in exc_info.value.message_dict


@pytest.mark.django_db
def test_ready_deployment_requires_successful_timestamped_verification():
    """Routing must not treat an unverified database declaration as deployable."""
    deployment = _job_deployment(
        validator=ValidatorFactory(),
        readiness_state=ExecutionDeploymentReadiness.READY,
    )

    with pytest.raises(ValidationError) as exc_info:
        deployment.full_clean()

    assert "readiness_state" in exc_info.value.message_dict


@pytest.mark.django_db(transaction=True)
def test_validator_has_at_most_one_deployment_in_each_active_routing_slot():
    """A database constraint protects activation from split-brain routing."""
    validator = ValidatorFactory()
    verified = _job_capabilities()
    first = _job_deployment(
        validator=validator,
        revision="r1",
        readiness_state=ExecutionDeploymentReadiness.READY,
        routing_role=ExecutionDeploymentRoutingRole.PRIMARY,
        verified_capabilities=deepcopy(verified),
        last_verification_succeeded=True,
        last_verified_at=timezone.now(),
    )
    first.full_clean()
    first.save()
    second = _job_deployment(
        validator=validator,
        revision="r2",
        readiness_state=ExecutionDeploymentReadiness.READY,
        routing_role=ExecutionDeploymentRoutingRole.PRIMARY,
        verified_capabilities=deepcopy(verified),
        last_verification_succeeded=True,
        last_verified_at=timezone.now(),
    )
    second.full_clean(exclude={"routing_role"}, validate_constraints=False)

    with pytest.raises(IntegrityError):
        ValidatorExecutionDeployment.objects.bulk_create([second])


@pytest.mark.django_db
def test_ready_deployment_requires_a_new_revision_for_identity_changes():
    """An accepted route cannot silently rewrite the provenance of later attempts."""
    verified = _job_capabilities()
    deployment = _job_deployment(
        validator=ValidatorFactory(),
        readiness_state=ExecutionDeploymentReadiness.READY,
        routing_role=ExecutionDeploymentRoutingRole.PRIMARY,
        verified_capabilities=deepcopy(verified),
        last_verification_succeeded=True,
        last_verified_at=timezone.now(),
    )
    deployment.save()
    deployment.backend_image_digest = "sha256:" + "b" * 64
    deployment.backend_image_ref = deployment.backend_image_ref.replace(
        DIGEST,
        deployment.backend_image_digest,
    )

    with pytest.raises(ValidationError, match="create a new revision"):
        deployment.save()


@pytest.mark.django_db
def test_ready_deployment_allows_fresh_verification_observations():
    """Operators may refresh readiness evidence without revising route identity."""
    verified = _job_capabilities()
    deployment = _job_deployment(
        validator=ValidatorFactory(),
        readiness_state=ExecutionDeploymentReadiness.READY,
        verified_capabilities=deepcopy(verified),
        last_verification_succeeded=True,
        last_verified_at=timezone.now(),
    )
    deployment.save()
    refreshed_details = deepcopy(deployment.last_verification_details)
    refreshed_details["checks"][0]["summary"] = "Provider identity re-verified."
    deployment.last_verification_details = refreshed_details
    deployment.last_verified_at = timezone.now()

    deployment.save()

    deployment.refresh_from_db()
    assert deployment.last_verification_details == refreshed_details


@pytest.mark.django_db
def test_retired_deployment_keeps_immutable_provider_provenance():
    """Cleanup must not make a historical deployment identity editable again."""
    deployment = _save_ready(
        _service_deployment(validator=ValidatorFactory()),
        role=ExecutionDeploymentRoutingRole.INACTIVE,
    )
    deployment = retire_execution_deployment(deployment)
    deployment.backend_image_digest = "sha256:" + "b" * 64
    deployment.backend_image_ref = deployment.backend_image_ref.replace(
        DIGEST,
        deployment.backend_image_digest,
    )

    with pytest.raises(ValidationError, match="create a new revision"):
        deployment.save()


@pytest.mark.django_db
def test_retired_deployment_rejects_capacity_mutation():
    """A deleted provider resource cannot acquire new stored warm capacity."""
    deployment = _save_ready(
        _service_deployment(validator=ValidatorFactory()),
        role=ExecutionDeploymentRoutingRole.INACTIVE,
    )
    deployment = retire_execution_deployment(deployment)

    with pytest.raises(ValueError, match="Only ready"):
        update_execution_deployment_capacity(
            deployment,
            minimum_instances=1,
            maximum_instances=10,
        )


@pytest.mark.django_db
def test_shared_job_resource_can_route_multiple_validator_contracts():
    """One backend Job may execute multiple FMU/library validator records."""
    first = ValidatorFactory()
    second = ValidatorFactory()
    job_name = "validibot-validator-backend-fmu"

    _job_deployment(validator=first, job_name=job_name).save()
    _job_deployment(validator=second, job_name=job_name).save()

    assert (
        ValidatorExecutionDeployment.objects.filter(
            provider_resource_name=(
                f"projects/{PROJECT_ID}/locations/{REGION}/jobs/{job_name}"
            )
        ).count()
        == EXPECTED_SHARED_DEPLOYMENT_COUNT
    )


@pytest.mark.django_db
def test_resolver_selects_primary_service_when_attempt_budget_fits():
    """Normal bounded work must use the explicitly activated Service route."""
    validator = ValidatorFactory()
    primary = _save_ready(
        _service_deployment(validator=validator),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )

    selected = resolve_execution_deployment(
        validator=validator,
        effective_budget_seconds=1200,
    )

    assert selected == primary


@pytest.mark.django_db
def test_service_budget_overflow_selects_long_running_job_before_dispatch():
    """Work over 25 minutes is planned onto Jobs rather than failed over later."""
    validator = ValidatorFactory()
    _save_ready(
        _service_deployment(validator=validator),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )
    job_capabilities = _job_capabilities()
    job_capabilities["maximum_execution_seconds"] = 3600
    compatibility = _save_ready(
        _job_deployment(
            validator=validator,
            declared_capabilities=job_capabilities,
            maximum_execution_seconds=3600,
        ),
        role=ExecutionDeploymentRoutingRole.LONG_RUNNING,
    )

    selected = resolve_execution_deployment(
        validator=validator,
        effective_budget_seconds=1800,
    )

    assert selected == compatibility


@pytest.mark.django_db
def test_blocked_primary_fails_closed_without_using_compatibility_job():
    """An operator block is authoritative and cannot become runtime failover."""
    validator = ValidatorFactory()
    primary = _save_ready(
        _service_deployment(validator=validator),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )
    primary.emergency_blocked = True
    primary.emergency_block_reason = "Operator investigation"
    primary.save(update_fields=["emergency_blocked", "emergency_block_reason"])
    _save_ready(
        _job_deployment(validator=validator),
        role=ExecutionDeploymentRoutingRole.LONG_RUNNING,
    )

    with pytest.raises(ExecutionDeploymentResolutionError, match="blocked"):
        resolve_execution_deployment(
            validator=validator,
            effective_budget_seconds=1200,
        )


@pytest.mark.django_db
def test_emergency_block_service_requires_reason_and_writes_audit_event():
    """A route must not be silently disabled outside an accountable operation."""
    deployment = _save_ready(
        _job_deployment(validator=ValidatorFactory()),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )

    with pytest.raises(ValueError, match="requires an operator reason"):
        set_execution_deployment_block(deployment, blocked=True)

    blocked = set_execution_deployment_block(
        deployment,
        blocked=True,
        reason="Provider incident under investigation",
    )

    assert blocked.emergency_blocked is True
    assert AuditLogEntry.objects.filter(
        action=AuditAction.VALIDATOR_DEPLOYMENT_BLOCKED,
        target_id=str(deployment.pk),
    ).exists()


@pytest.mark.django_db
def test_service_retirement_requires_inactive_cold_and_drained_deployment():
    """Cleanup must never delete a route that can still launch or callback."""
    deployment = _save_ready(
        _service_deployment(validator=ValidatorFactory()),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )

    with pytest.raises(ExecutionDeploymentResolutionError, match="routing slot"):
        ensure_execution_deployment_can_retire(deployment)

    deployment.routing_role = ExecutionDeploymentRoutingRole.INACTIVE
    deployment.save(update_fields=["routing_role", "modified"])
    attempt = ExecutionAttemptFactory(deployment=deployment, state="RUNNING")
    with pytest.raises(ExecutionDeploymentResolutionError, match="nonterminal"):
        ensure_execution_deployment_can_retire(deployment)

    attempt.state = "COMPLETED"
    attempt.save(update_fields=["state", "modified"])
    retired = retire_execution_deployment(deployment)

    assert retired.readiness_state == ExecutionDeploymentReadiness.RETIRED
    assert AuditLogEntry.objects.filter(
        action=AuditAction.VALIDATOR_DEPLOYMENT_RETIRED,
        target_id=str(deployment.pk),
    ).exists()


@pytest.mark.django_db
def test_managed_attempt_pins_route_snapshot_and_absolute_deadline():
    """Dispatch evidence must be complete before the first provider API call."""
    validator = ValidatorFactory()
    deployment = _save_ready(
        _job_deployment(validator=validator),
        role=ExecutionDeploymentRoutingRole.PRIMARY,
    )
    step_run = ValidationStepRunFactory()
    before = timezone.now()

    attempt, created = get_or_create_execution_attempt(
        step_run,
        validator=validator,
        managed=True,
        effective_budget_seconds=ATTEMPT_BUDGET_SECONDS,
    )

    assert created is True
    assert attempt.deployment == deployment
    assert attempt.deployment_snapshot["deployment_id"] == str(deployment.pk)
    assert attempt.deployment_snapshot["backend_image_digest"] == DIGEST
    assert attempt.provider_resource_name == deployment.provider_resource_name
    assert attempt.backend_image_digest == DIGEST
    assert attempt.timeout_at is not None
    elapsed_seconds = (attempt.timeout_at - before).total_seconds()
    expected_deadline_seconds = ATTEMPT_BUDGET_SECONDS + JOB_FINALIZATION_MARGIN_SECONDS
    assert (
        expected_deadline_seconds - DEADLINE_TOLERANCE_SECONDS
        <= elapsed_seconds
        <= expected_deadline_seconds + DEADLINE_TOLERANCE_SECONDS
    )
    assert attempt.retry_policy_snapshot["maximum_provider_dispatches"] == 1
