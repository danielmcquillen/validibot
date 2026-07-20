"""Typed, secret-free configuration contracts for managed validator routes.

The deployment model stores provider configuration as JSON so future cloud
providers do not require a new group of nullable database columns.  This
module is the strict boundary around that JSON: unknown keys are rejected,
only non-secret provider facts are accepted, and each deployment kind has a
small explicit schema.

These contracts intentionally contain no credential, token, or private-key
fields.  Workload identity and Secret Manager references are configured by
operators outside the database; the model records only the immutable service
account identity expected at runtime.
"""

from __future__ import annotations

import re
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import UUID4
from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator
from pydantic import model_validator

from validibot.validations.constants import CallbackAuthenticationMethod
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionDeploymentRoutingRole
from validibot.validations.constants import ExecutionProviderType
from validibot.validations.constants import ExecutionShape
from validibot.validations.constants import ProviderCancellationCapability
from validibot.validations.constants import ProviderStatusLookupCapability
from validibot.validations.constants import RuntimeStorageIsolation
from validibot.validations.constants import StorageCapabilityMode

_GCP_PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_GCP_LOCATION_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_CLOUD_RUN_RESOURCE_PATTERN = re.compile(r"^[a-z]([a-z0-9-]{0,61}[a-z0-9])?$")
_SERVICE_ACCOUNT_PATTERN = re.compile(
    r"^[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]"
    r"\.iam\.gserviceaccount\.com$"
)
_MAX_ARCHITECTURE_NAME_LENGTH = 32

ProjectId = Annotated[str, Field(min_length=6, max_length=30)]
Location = Annotated[str, Field(min_length=1, max_length=63)]
ResourceName = Annotated[str, Field(min_length=1, max_length=63)]
ServiceAccountEmail = Annotated[str, Field(min_length=30, max_length=254)]


def _validate_https_origin(value: str) -> str:
    """Return a normalized HTTPS origin without accepting embedded secrets."""
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        msg = "must be an HTTPS origin without credentials, path, query, or fragment"
        raise ValueError(msg)
    return value.removesuffix("/")


class _GCPProviderConfig(BaseModel):
    """Shared non-secret coordinates for a managed GCP deployment."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    project_id: ProjectId
    region: Location
    runtime_service_account: ServiceAccountEmail

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        """Require the documented lowercase Google Cloud project-id shape."""
        if not _GCP_PROJECT_ID_PATTERN.fullmatch(value):
            msg = "must be a valid lowercase Google Cloud project id"
            raise ValueError(msg)
        return value

    @field_validator("region")
    @classmethod
    def validate_region(cls, value: str) -> str:
        """Reject malformed locations before a provider API call is attempted."""
        if not _GCP_LOCATION_PATTERN.fullmatch(value):
            msg = "must be a valid lowercase Google Cloud location"
            raise ValueError(msg)
        return value

    @field_validator("runtime_service_account")
    @classmethod
    def validate_runtime_service_account(cls, value: str) -> str:
        """Accept an identity address, never a credential or credential blob."""
        if not _SERVICE_ACCOUNT_PATTERN.fullmatch(value):
            msg = "must be a Google service-account email address"
            raise ValueError(msg)
        return value


class CloudRunJobProviderConfig(_GCPProviderConfig):
    """Provider configuration for one immutable Cloud Run Job route."""

    job_name: ResourceName

    @field_validator("job_name")
    @classmethod
    def validate_job_name(cls, value: str) -> str:
        """Require a Cloud Run-compatible resource name."""
        if not _CLOUD_RUN_RESOURCE_PATTERN.fullmatch(value):
            msg = "must be a valid lowercase Cloud Run Job name"
            raise ValueError(msg)
        return value

    @property
    def canonical_resource_name(self) -> str:
        """Return the provider-addressable Job resource name."""
        return (
            f"projects/{self.project_id}/locations/{self.region}/jobs/{self.job_name}"
        )


class CloudRunServiceProviderConfig(_GCPProviderConfig):
    """Provider configuration for one immutable private Cloud Run Service."""

    service_name: ResourceName
    service_url: Annotated[str, Field(min_length=9, max_length=2048)]
    authentication_audience: Annotated[str, Field(min_length=9, max_length=2048)]
    invoker_service_account: ServiceAccountEmail

    @field_validator("service_name")
    @classmethod
    def validate_service_name(cls, value: str) -> str:
        """Require a Cloud Run-compatible resource name."""
        if not _CLOUD_RUN_RESOURCE_PATTERN.fullmatch(value):
            msg = "must be a valid lowercase Cloud Run Service name"
            raise ValueError(msg)
        return value

    @field_validator("service_url", "authentication_audience")
    @classmethod
    def validate_origin(cls, value: str) -> str:
        """Keep credentials and request-specific data out of persisted routes."""
        return _validate_https_origin(value)

    @field_validator("invoker_service_account")
    @classmethod
    def validate_invoker_service_account(cls, value: str) -> str:
        """Record only the invoker identity, not any credential material."""
        if not _SERVICE_ACCOUNT_PATTERN.fullmatch(value):
            msg = "must be a Google service-account email address"
            raise ValueError(msg)
        return value

    @property
    def canonical_resource_name(self) -> str:
        """Return the provider-addressable Service resource name."""
        return (
            f"projects/{self.project_id}/locations/{self.region}/services/"
            f"{self.service_name}"
        )


ProviderConfiguration = CloudRunJobProviderConfig | CloudRunServiceProviderConfig


class DeploymentCapabilities(BaseModel):
    """Portable behavior contract declared and verified for one route."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    runtime_contract_version: Annotated[str, Field(min_length=1, max_length=64)]
    maximum_execution_seconds: Annotated[int, Field(ge=1, le=86400)]
    execution_shape: ExecutionShape
    status_lookup: ProviderStatusLookupCapability
    cancellation: ProviderCancellationCapability
    storage_capability: StorageCapabilityMode
    storage_isolation: RuntimeStorageIsolation
    architectures: Annotated[tuple[str, ...], Field(min_length=1)]
    maximum_cpu_millis: Annotated[int, Field(ge=1, le=256000)]
    maximum_memory_mib: Annotated[int, Field(ge=1, le=1048576)]
    callback_authentication: CallbackAuthenticationMethod

    @field_validator("architectures")
    @classmethod
    def validate_architectures(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Keep architecture identifiers bounded, normalized, and unambiguous."""
        normalized = tuple(item.strip().lower() for item in value)
        if any(
            not item
            or len(item) > _MAX_ARCHITECTURE_NAME_LENGTH
            or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", item)
            for item in normalized
        ):
            msg = "architectures must contain normalized platform identifiers"
            raise ValueError(msg)
        if len(set(normalized)) != len(normalized):
            msg = "architectures must not contain duplicates"
            raise ValueError(msg)
        return normalized

    @model_validator(mode="after")
    def validate_storage_isolation(self):
        """Attempt-scoped capability must truthfully declare attempt isolation."""
        if (
            self.storage_capability
            in {
                StorageCapabilityMode.LOCAL_ATTEMPT_MOUNT,
                StorageCapabilityMode.GCS_DOWNSCOPED_TOKEN,
                StorageCapabilityMode.SERVER_MEDIATED_BROKER,
            }
            and self.storage_isolation != RuntimeStorageIsolation.ATTEMPT_SCOPED
        ):
            msg = "attempt-scoped storage capability requires attempt-scoped isolation"
            raise ValueError(msg)
        return self


class DeploymentVerificationCheck(BaseModel):
    """One bounded, operator-safe observation from deployment verification."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")]
    succeeded: bool
    summary: Annotated[str, Field(max_length=500)] = ""


class DeploymentVerificationDetails(BaseModel):
    """Secret-free provider observations retained with readiness state."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    observed_provider_revision: Annotated[str, Field(min_length=1, max_length=128)]
    observed_resource_name: Annotated[str, Field(min_length=1, max_length=512)]
    observed_image_digest: Annotated[
        str,
        Field(pattern=r"^sha256:[0-9a-f]{64}$"),
    ]
    checks: Annotated[tuple[DeploymentVerificationCheck, ...], Field(min_length=1)]


class DeploymentRouteSnapshot(BaseModel):
    """Immutable, secret-free deployment facts copied onto one attempt.

    The foreign key is the convenient relational link.  This snapshot is the
    durable evidence boundary: later routing, readiness, block, or display-name
    changes cannot alter the operational facts an attempt selected.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    schema_version: Annotated[int, Field(ge=1, le=1)] = 1
    deployment_id: UUID4
    validator_id: int
    selected_at: AwareDatetime
    provider_type: ExecutionProviderType
    deployment_kind: ExecutionDeploymentKind
    deployment_revision: Annotated[str, Field(min_length=1, max_length=128)]
    provider_resource_name: Annotated[str, Field(min_length=1, max_length=512)]
    route: Annotated[str, Field(max_length=2048)] = ""
    authentication_audience: Annotated[str, Field(max_length=2048)] = ""
    backend_release_identity: Annotated[str, Field(min_length=1, max_length=128)]
    backend_image_ref: Annotated[str, Field(min_length=1, max_length=512)]
    backend_image_digest: Annotated[
        str,
        Field(pattern=r"^sha256:[0-9a-f]{64}$"),
    ]
    expected_runtime_identity: ServiceAccountEmail
    routing_role: ExecutionDeploymentRoutingRole
    declared_capabilities: DeploymentCapabilities
    verified_capabilities: DeploymentCapabilities
    maximum_execution_seconds: Annotated[int, Field(ge=1, le=86400)]
    request_timeout_seconds: Annotated[int | None, Field(ge=1, le=86400)] = None
    dispatch_timeout_seconds: Annotated[int, Field(ge=1, le=86400)]
    minimum_instances: Annotated[int, Field(ge=0)]
    maximum_instances: Annotated[int | None, Field(ge=1)] = None
    concurrency: Annotated[int | None, Field(ge=1)] = None


def parse_deployment_capabilities(
    *,
    deployment_kind: str,
    capabilities: dict,
) -> DeploymentCapabilities:
    """Validate capabilities and their provider-shape invariants."""
    try:
        kind = ExecutionDeploymentKind(deployment_kind)
    except ValueError as exc:
        msg = f"Unsupported execution deployment kind: {deployment_kind!r}"
        raise ValueError(msg) from exc

    parsed = DeploymentCapabilities.model_validate(capabilities)
    expected_shape = {
        ExecutionDeploymentKind.CLOUD_RUN_JOB: ExecutionShape.JOB,
        ExecutionDeploymentKind.CLOUD_RUN_SERVICE: ExecutionShape.REQUEST,
    }[kind]
    if parsed.execution_shape != expected_shape:
        msg = (
            f"{kind.label} deployments must declare the "
            f"{expected_shape.label} execution shape"
        )
        raise ValueError(msg)
    if (
        kind == ExecutionDeploymentKind.CLOUD_RUN_SERVICE
        and parsed.status_lookup != ProviderStatusLookupCapability.UNSUPPORTED
    ):
        msg = "Cloud Run Service deployments cannot declare status lookup support"
        raise ValueError(msg)
    return parsed


def parse_provider_configuration(
    *,
    provider_type: str,
    deployment_kind: str,
    configuration: dict,
) -> ProviderConfiguration:
    """Validate provider JSON using the exact provider/kind contract.

    Unsupported combinations fail closed.  This is important for future
    providers: adding an enum value alone must never make arbitrary JSON
    launchable.
    """
    try:
        provider = ExecutionProviderType(provider_type)
        kind = ExecutionDeploymentKind(deployment_kind)
    except ValueError as exc:
        msg = (
            "Unsupported execution provider/deployment kind: "
            f"{provider_type!r}/{deployment_kind!r}"
        )
        raise ValueError(msg) from exc

    schemas: dict[
        tuple[ExecutionProviderType, ExecutionDeploymentKind],
        type[ProviderConfiguration],
    ] = {
        (
            ExecutionProviderType.GCP,
            ExecutionDeploymentKind.CLOUD_RUN_JOB,
        ): CloudRunJobProviderConfig,
        (
            ExecutionProviderType.GCP,
            ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
        ): CloudRunServiceProviderConfig,
    }
    schema = schemas.get((provider, kind))
    if schema is None:
        msg = (
            "Unsupported execution provider/deployment kind: "
            f"{provider_type!r}/{deployment_kind!r}"
        )
        raise ValueError(msg)
    return schema.model_validate(configuration)
