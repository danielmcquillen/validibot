"""
Execution backend factory.

Managed attempts select an adapter from their immutable provider deployment.
Unmanaged/local execution falls back to the deployment target and the legacy
``VALIDATOR_RUNNER`` override.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

from django.conf import settings

from validibot.core.constants import DeploymentTarget
from validibot.core.deployment import get_deployment_target

if TYPE_CHECKING:
    from validibot.validations.models import ValidatorExecutionDeployment
    from validibot.validations.services.execution.base import ExecutionBackend

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ManagedExecutionBackendRoute:
    """One authoritative provider/deployment-kind adapter registration."""

    backend_class: type[ExecutionBackend]
    runner_type: str


@lru_cache(maxsize=1)
def _managed_execution_backend_routes() -> dict[
    tuple[str, str], ManagedExecutionBackendRoute
]:
    """Build the process's authoritative managed adapter registrations."""
    from validibot.validations.constants import ExecutionDeploymentKind
    from validibot.validations.constants import ExecutionProviderType
    from validibot.validations.services.execution.gcp import (
        CloudRunJobsExecutionBackend,
    )
    from validibot.validations.services.execution.gcp_service import (
        CloudRunServiceExecutionBackend,
    )

    return {
        (
            ExecutionProviderType.GCP,
            ExecutionDeploymentKind.CLOUD_RUN_JOB,
        ): ManagedExecutionBackendRoute(
            backend_class=CloudRunJobsExecutionBackend,
            runner_type="CloudRunJobsExecutionBackend",
        ),
        (
            ExecutionProviderType.GCP,
            ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
        ): ManagedExecutionBackendRoute(
            backend_class=CloudRunServiceExecutionBackend,
            runner_type="CloudRunServiceExecutionBackend",
        ),
    }


def get_managed_execution_backend_route(
    deployment: ValidatorExecutionDeployment,
) -> ManagedExecutionBackendRoute:
    """Resolve the adapter and stable attempt identifier for a pinned route."""
    key = (deployment.provider_type, deployment.deployment_kind)
    route = _managed_execution_backend_routes().get(key)
    if route is None:
        msg = (
            "No execution backend implemented for managed deployment: "
            f"{deployment.provider_type}/{deployment.deployment_kind}"
        )
        raise ValueError(msg)
    return route


def get_execution_backend(
    deployment: ValidatorExecutionDeployment | None = None,
) -> ExecutionBackend:
    """
    Get the execution backend for a pinned route or unmanaged target.

    A supplied managed deployment is authoritative. ``VALIDATOR_RUNNER`` only
    influences the unmanaged/local fallback when no deployment is supplied.

    Returns:
        ExecutionBackend instance for the current deployment target.

    Raises:
        ValueError: If DEPLOYMENT_TARGET is not set or no backend exists.
    """
    if deployment is not None:
        route = get_managed_execution_backend_route(deployment)
        return route.backend_class(deployment=deployment)

    return _get_default_execution_backend()


@lru_cache(maxsize=1)
def _get_default_execution_backend() -> ExecutionBackend:
    """Return the cached deployment-target backend for unmanaged execution."""
    # Import here to avoid circular imports
    from validibot.validations.services.execution.docker_compose import (
        DockerComposeExecutionBackend,
    )
    from validibot.validations.services.execution.gcp import (
        CloudRunJobsExecutionBackend,
    )

    # Check for explicit VALIDATOR_RUNNER override first
    runner_override = getattr(settings, "VALIDATOR_RUNNER", None)
    if runner_override:
        logger.debug("Using explicit VALIDATOR_RUNNER override: %s", runner_override)
        backends_by_name: dict[str, type[ExecutionBackend]] = {
            "docker": DockerComposeExecutionBackend,
            "google_cloud_run": CloudRunJobsExecutionBackend,
        }
        backend_class = backends_by_name.get(runner_override)
        if not backend_class:
            available = ", ".join(backends_by_name.keys())
            msg = f"Unknown VALIDATOR_RUNNER: {runner_override}. Available: {available}"
            raise ValueError(msg)
    else:
        # Use DEPLOYMENT_TARGET to select backend
        target = get_deployment_target()

        backends: dict[DeploymentTarget, type[ExecutionBackend]] = {
            DeploymentTarget.TEST: DockerComposeExecutionBackend,
            DeploymentTarget.LOCAL_DOCKER_COMPOSE: DockerComposeExecutionBackend,
            DeploymentTarget.SELF_HOSTED: DockerComposeExecutionBackend,
            DeploymentTarget.GCP: CloudRunJobsExecutionBackend,
            # AWS not yet implemented
        }

        backend_class = backends.get(target)
        if not backend_class:
            msg = f"No execution backend implemented for deployment target: {target}"
            raise ValueError(msg)

    backend = backend_class()

    logger.info(
        "Initialized execution backend: %s (async=%s) for DEPLOYMENT_TARGET=%s",
        backend.backend_name,
        backend.is_async,
        getattr(settings, "DEPLOYMENT_TARGET", "not set"),
    )

    return backend


def clear_backend_cache() -> None:
    """Clear the cached backend instance."""
    _get_default_execution_backend.cache_clear()
