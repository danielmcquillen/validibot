"""Tests for provider-selectable validator deployment configuration.

The database will retain provider-specific configuration as JSON.  These tests
protect the typed boundary that makes that design safe: known GCP Job and
Service shapes are accepted, malformed provider coordinates fail before
dispatch, and secret-like or otherwise unknown fields cannot be persisted.
"""

import pytest
from pydantic import ValidationError

from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionProviderType
from validibot.validations.constants import ExecutionShape
from validibot.validations.constants import ProviderStatusLookupCapability
from validibot.validations.services.execution.deployment_schemas import (
    CloudRunJobProviderConfig,
)
from validibot.validations.services.execution.deployment_schemas import (
    CloudRunServiceProviderConfig,
)
from validibot.validations.services.execution.deployment_schemas import (
    DeploymentVerificationDetails,
)
from validibot.validations.services.execution.deployment_schemas import (
    parse_deployment_capabilities,
)
from validibot.validations.services.execution.deployment_schemas import (
    parse_provider_configuration,
)

PROJECT_ID = "validibot-prod"
REGION = "australia-southeast1"
RUNTIME_IDENTITY = "validator-runtime@validibot-prod.iam.gserviceaccount.com"
INVOKER_IDENTITY = "validator-invoker@validibot-prod.iam.gserviceaccount.com"


def test_cloud_run_job_configuration_has_canonical_resource_identity():
    """A Job route must retain enough safe data to address the exact resource."""
    config = parse_provider_configuration(
        provider_type=ExecutionProviderType.GCP,
        deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_JOB,
        configuration={
            "project_id": PROJECT_ID,
            "region": REGION,
            "job_name": "validibot-energyplus",
            "runtime_service_account": RUNTIME_IDENTITY,
        },
    )

    assert isinstance(config, CloudRunJobProviderConfig)
    assert config.canonical_resource_name == (
        "projects/validibot-prod/locations/australia-southeast1/jobs/"
        "validibot-energyplus"
    )


def test_cloud_run_service_configuration_normalizes_origins():
    """Stable Service routing stores origins, not request-specific URLs."""
    config = parse_provider_configuration(
        provider_type=ExecutionProviderType.GCP,
        deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
        configuration={
            "project_id": PROJECT_ID,
            "region": REGION,
            "service_name": "validibot-shacl-r3",
            "service_url": "https://validibot-shacl-r3.example.run.app/",
            "authentication_audience": ("https://validibot-shacl-r3.example.run.app/"),
            "runtime_service_account": RUNTIME_IDENTITY,
            "invoker_service_account": INVOKER_IDENTITY,
        },
    )

    assert isinstance(config, CloudRunServiceProviderConfig)
    assert config.service_url == "https://validibot-shacl-r3.example.run.app"
    assert config.authentication_audience == config.service_url
    assert config.canonical_resource_name.endswith("/services/validibot-shacl-r3")


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("access_token", "secret"),
        ("credentials", {"private_key": "secret"}),
        ("service_account_key", "secret"),
    ],
)
def test_provider_configuration_rejects_unknown_secret_fields(
    field_name,
    field_value,
):
    """Credentials must remain in workload identity or a secret store, never JSON."""
    configuration = {
        "project_id": PROJECT_ID,
        "region": REGION,
        "job_name": "validibot-fmu",
        "runtime_service_account": RUNTIME_IDENTITY,
        field_name: field_value,
    }

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        parse_provider_configuration(
            provider_type=ExecutionProviderType.GCP,
            deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_JOB,
            configuration=configuration,
        )


@pytest.mark.parametrize(
    "service_url",
    [
        "http://validator.example.run.app",
        "https://user:password@validator.example.run.app",
        "https://validator.example.run.app/execute",
        "https://validator.example.run.app?token=secret",
    ],
)
def test_cloud_run_service_rejects_unsafe_or_request_specific_urls(service_url):
    """Routes must be HTTPS origins and must not become a covert secret store."""
    with pytest.raises(ValidationError, match="must be an HTTPS origin"):
        parse_provider_configuration(
            provider_type=ExecutionProviderType.GCP,
            deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
            configuration={
                "project_id": PROJECT_ID,
                "region": REGION,
                "service_name": "validibot-schematron-r2",
                "service_url": service_url,
                "authentication_audience": (
                    "https://validibot-schematron-r2.example.run.app"
                ),
                "runtime_service_account": RUNTIME_IDENTITY,
                "invoker_service_account": INVOKER_IDENTITY,
            },
        )


def test_provider_configuration_rejects_unknown_provider_kind_pairs():
    """A new enum or provider name cannot silently make arbitrary JSON runnable."""
    with pytest.raises(ValueError, match="Unsupported execution provider"):
        parse_provider_configuration(
            provider_type="AWS",
            deployment_kind="ECS_TASK",
            configuration={},
        )


@pytest.mark.parametrize(
    ("field_name", "field_value"),
    [
        ("project_id", "Invalid_Project"),
        ("region", "Australia Southeast 1"),
        ("job_name", "Validator Job"),
        ("runtime_service_account", "not-an-identity"),
    ],
)
def test_cloud_run_job_rejects_malformed_provider_coordinates(
    field_name,
    field_value,
):
    """Malformed provider identity fails locally instead of during dispatch."""
    configuration = {
        "project_id": PROJECT_ID,
        "region": REGION,
        "job_name": "validibot-energyplus",
        "runtime_service_account": RUNTIME_IDENTITY,
    }
    configuration[field_name] = field_value

    with pytest.raises(ValidationError):
        parse_provider_configuration(
            provider_type=ExecutionProviderType.GCP,
            deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_JOB,
            configuration=configuration,
        )


def _capabilities(**overrides):
    """Build a complete capability document so each test changes one contract."""
    capabilities = {
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
    capabilities.update(overrides)
    return capabilities


def test_cloud_run_job_capabilities_describe_queryable_job_execution():
    """The compatibility route must expose the reconciliation behavior Jobs have."""
    capabilities = parse_deployment_capabilities(
        deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_JOB,
        capabilities=_capabilities(),
    )

    assert capabilities.execution_shape == ExecutionShape.JOB
    assert capabilities.status_lookup == ProviderStatusLookupCapability.SUPPORTED
    assert capabilities.architectures == ("linux-amd64",)


def test_cloud_run_service_capabilities_reject_provider_status_lookup():
    """A request-driven Service must not promise a durable provider execution API."""
    with pytest.raises(ValueError, match="cannot declare status lookup support"):
        parse_deployment_capabilities(
            deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
            capabilities=_capabilities(execution_shape="REQUEST"),
        )


def test_deployment_capabilities_reject_shape_mismatch():
    """Routing cannot infer a Job contract from a request-driven capability claim."""
    with pytest.raises(ValueError, match="must declare the Provider job"):
        parse_deployment_capabilities(
            deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_JOB,
            capabilities=_capabilities(execution_shape="REQUEST"),
        )


def test_deployment_capabilities_reject_false_storage_isolation():
    """A downscoped token cannot be labelled with unsupported isolation."""
    with pytest.raises(ValidationError, match="requires attempt-scoped isolation"):
        parse_deployment_capabilities(
            deployment_kind=ExecutionDeploymentKind.CLOUD_RUN_JOB,
            capabilities=_capabilities(
                storage_isolation="unsupported",
            ),
        )


def test_verification_details_reject_credentials_and_unknown_provider_output():
    """Readiness evidence stores bounded observations, never raw API responses."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DeploymentVerificationDetails.model_validate(
            {
                "observed_provider_revision": "r3",
                "observed_resource_name": (
                    "projects/validibot-prod/locations/australia-southeast1/"
                    "jobs/validibot-energyplus"
                ),
                "observed_image_digest": "sha256:" + "a" * 64,
                "checks": [
                    {
                        "code": "provider.resource",
                        "succeeded": True,
                        "summary": "Provider identity matched.",
                    }
                ],
                "access_token": "must-not-be-stored",
            }
        )
