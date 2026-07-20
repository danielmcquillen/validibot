"""Tests for importing existing Cloud Run Jobs as managed deployment routes.

The importer is the Phase 1 bridge: production Jobs already exist, so Validibot
must read and verify their exact immutable facts without redeploying them or
manufacturing provenance for older attempts.  These tests cover digest and
resource validation, shared provider resources, idempotency, activation, and
the four-backend management-command inventory.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.management import call_command

from validibot.validations.constants import ExecutionDeploymentRoutingRole
from validibot.validations.constants import ValidationType
from validibot.validations.models import ValidatorExecutionDeployment
from validibot.validations.services.execution.gcp_job_import import GCPJobImportError
from validibot.validations.services.execution.gcp_job_import import (
    observe_cloud_run_job,
)
from validibot.validations.services.execution.gcp_job_import import (
    register_observed_job_deployment,
)
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import ValidatorFactory

PROJECT_ID = "validibot-prod"
REGION = "australia-southeast1"
RUNTIME_IDENTITY = "validator-runtime@validibot-prod.iam.gserviceaccount.com"
DIGEST = "sha256:" + "d" * 64
REVISION = "3b4ef06"
MANAGED_BACKEND_COUNT = 4
JOB_TIMEOUT_SECONDS = 3600
JOB_CPU_MILLIS = 2000
JOB_MEMORY_MIB = 4096


def _job(resource_name: str, *, image_ref: str | None = None):
    """Return the minimal provider-shaped Job object consumed by the importer."""
    container = SimpleNamespace(
        image=image_ref or f"{REGION}-docker.pkg.dev/x/backend@{DIGEST}",
        resources=SimpleNamespace(limits={"cpu": "2", "memory": "4Gi"}),
    )
    task_template = SimpleNamespace(
        containers=[container],
        service_account=RUNTIME_IDENTITY,
        timeout=SimpleNamespace(seconds=JOB_TIMEOUT_SECONDS),
    )
    return SimpleNamespace(
        name=resource_name,
        reconciling=False,
        labels={"revision": REVISION},
        template=SimpleNamespace(template=task_template),
    )


def _resource(job_name: str) -> str:
    """Return the canonical provider name for a test Job."""
    return f"projects/{PROJECT_ID}/locations/{REGION}/jobs/{job_name}"


def test_observation_extracts_exact_digest_identity_and_resource_limits():
    """Readiness facts must come from the live provider spec, not defaults."""
    resource_name = _resource("validibot-validator-backend-energyplus")

    observation = observe_cloud_run_job(
        _job(resource_name),
        expected_resource_name=resource_name,
    )

    assert observation.resource_name == resource_name
    assert observation.image_digest == DIGEST
    assert observation.maximum_execution_seconds == JOB_TIMEOUT_SECONDS
    assert observation.maximum_cpu_millis == JOB_CPU_MILLIS
    assert observation.maximum_memory_mib == JOB_MEMORY_MIB


def test_observation_rejects_floating_image_tag():
    """A mutable provider image must never be registered as verified provenance."""
    resource_name = _resource("validibot-validator-backend-fmu")

    with pytest.raises(GCPJobImportError, match="not pinned"):
        observe_cloud_run_job(
            _job(resource_name, image_ref="example.invalid/fmu:latest"),
            expected_resource_name=resource_name,
        )


@pytest.mark.django_db
def test_registration_is_idempotent_and_does_not_rewrite_historical_attempts():
    """Re-running import converges while legacy attempts remain explicitly unknown."""
    validator = ValidatorFactory(validation_type=ValidationType.FMU)
    attempt = ExecutionAttemptFactory()
    resource_name = _resource("validibot-validator-backend-fmu")
    observation = observe_cloud_run_job(
        _job(resource_name),
        expected_resource_name=resource_name,
    )

    first, first_created = register_observed_job_deployment(
        validator=validator,
        project_id=PROJECT_ID,
        region=REGION,
        observation=observation,
        activate_primary=True,
    )
    second, second_created = register_observed_job_deployment(
        validator=validator,
        project_id=PROJECT_ID,
        region=REGION,
        observation=observation,
        activate_primary=True,
    )

    assert first_created is True
    assert second_created is False
    assert second.pk == first.pk
    assert second.routing_role == ExecutionDeploymentRoutingRole.PRIMARY
    attempt.refresh_from_db()
    assert attempt.deployment_id is None
    assert attempt.deployment_snapshot == {}


@pytest.mark.django_db
@patch(
    "validibot.validations.management.commands.sync_gcp_validator_deployments."
    "_resolve_cloud_run_job_name",
    side_effect=lambda validation_type: (
        f"validibot-validator-backend-{validation_type.lower()}"
    ),
)
@patch(
    "validibot.validations.management.commands.sync_gcp_validator_deployments."
    "run_v2.JobsClient",
)
def test_command_imports_and_activates_all_four_current_backend_types(
    jobs_client_class,
    resolve_job_name,
    settings,
):
    """The operator command must cover every current release-enabled backend."""
    settings.GCP_PROJECT_ID = PROJECT_ID
    settings.GCP_REGION = REGION
    for validation_type in (
        ValidationType.ENERGYPLUS,
        ValidationType.FMU,
        ValidationType.SHACL,
        ValidationType.SCHEMATRON,
    ):
        ValidatorFactory(validation_type=validation_type)
    jobs_client_class.return_value.get_job.side_effect = lambda *, name: _job(name)

    call_command("sync_gcp_validator_deployments", "--activate-primary")

    routes = ValidatorExecutionDeployment.objects.filter(
        routing_role=ExecutionDeploymentRoutingRole.PRIMARY
    )
    assert routes.count() == MANAGED_BACKEND_COUNT
    assert {route.validator.validation_type for route in routes} == {
        ValidationType.ENERGYPLUS,
        ValidationType.FMU,
        ValidationType.SHACL,
        ValidationType.SCHEMATRON,
    }
    assert resolve_job_name.call_count == MANAGED_BACKEND_COUNT
