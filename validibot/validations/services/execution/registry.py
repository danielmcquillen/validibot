"""
Execution backend factory.

Provides a factory function to get the appropriate execution backend
based on the DEPLOYMENT_TARGET setting.

The VALIDATOR_RUNNER setting can override the default if needed.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

from django.conf import settings

from validibot.core.constants import DeploymentTarget
from validibot.core.deployment import get_deployment_target

if TYPE_CHECKING:
    from validibot.validations.services.execution.base import ExecutionBackend

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_execution_backend() -> ExecutionBackend:
    """
    Get the execution backend for the configured deployment target.

    The VALIDATOR_RUNNER setting can override the default selection.

    Returns:
        ExecutionBackend instance for the current deployment target.

    Raises:
        ValueError: If DEPLOYMENT_TARGET is not set or no backend exists.
    """
    # Import here to avoid circular imports
    from validibot.validations.services.execution.gcp import GCPExecutionBackend
    from validibot.validations.services.execution.self_hosted import (
        SelfHostedExecutionBackend,
    )

    # Check for explicit VALIDATOR_RUNNER override first
    runner_override = getattr(settings, "VALIDATOR_RUNNER", None)
    if runner_override:
        logger.debug("Using explicit VALIDATOR_RUNNER override: %s", runner_override)
        backends_by_name: dict[str, type[ExecutionBackend]] = {
            "docker": SelfHostedExecutionBackend,
            "google_cloud_run": GCPExecutionBackend,
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
            DeploymentTarget.TEST: SelfHostedExecutionBackend,
            DeploymentTarget.LOCAL_DOCKER_COMPOSE: SelfHostedExecutionBackend,
            DeploymentTarget.DOCKER_COMPOSE: SelfHostedExecutionBackend,
            DeploymentTarget.GCP: GCPExecutionBackend,
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
    get_execution_backend.cache_clear()
