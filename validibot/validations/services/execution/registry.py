"""
Execution backend registry.

This module provides a factory function to get the appropriate execution backend
based on configuration. The backend is cached as a singleton for efficiency.

## Configuration

The backend is selected via the `VALIDATOR_RUNNER` setting:

- `"docker"` → SelfHostedExecutionBackend (sync, local Docker)
- `"google_cloud_run"` → GCPExecutionBackend (async, Cloud Run Jobs)
- `"aws_batch"` → AWSBatchExecutionBackend (future)

## Auto-Detection

If `VALIDATOR_RUNNER` is not set, the registry will auto-detect:
- If `GCP_PROJECT_ID` is set → GCPExecutionBackend
- Otherwise → SelfHostedExecutionBackend (default)

## Usage

```python
from validibot.validations.services.execution import get_execution_backend

backend = get_execution_backend()
response = backend.execute(request)
```
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING
from typing import Any

from django.conf import settings

if TYPE_CHECKING:
    from validibot.validations.services.execution.base import ExecutionBackend

logger = logging.getLogger(__name__)


# Registered backend classes
# Using Any type for runtime since ExecutionBackend is only imported for type checking
_BACKEND_CLASSES: dict[str, type[Any]] = {}


def register_backend(name: str, backend_class: type[ExecutionBackend]) -> None:
    """
    Register an execution backend class.

    Args:
        name: Backend name (e.g., "docker", "google_cloud_run").
        backend_class: Backend class to register.
    """
    _BACKEND_CLASSES[name] = backend_class
    logger.debug("Registered execution backend: %s -> %s", name, backend_class.__name__)


def _register_default_backends() -> None:
    """Register the default backends."""
    # Self-hosted (Docker)
    from validibot.validations.services.execution.self_hosted import (
        SelfHostedExecutionBackend,
    )

    register_backend("docker", SelfHostedExecutionBackend)

    # GCP (Cloud Run Jobs)
    from validibot.validations.services.execution.gcp import GCPExecutionBackend

    register_backend("google_cloud_run", GCPExecutionBackend)


def _get_backend_name() -> str:
    """
    Determine which backend to use based on settings.

    Returns:
        Backend name string.
    """
    # Explicit setting takes precedence
    runner = getattr(settings, "VALIDATOR_RUNNER", None)
    if runner:
        return runner

    # Auto-detect based on environment
    if getattr(settings, "GCP_PROJECT_ID", None):
        logger.info(
            "Auto-detected GCP environment (GCP_PROJECT_ID set), "
            "using google_cloud_run backend"
        )
        return "google_cloud_run"

    # Default to self-hosted Docker
    logger.info(
        "No VALIDATOR_RUNNER set and no cloud environment detected, "
        "using docker backend (self-hosted)"
    )
    return "docker"


@lru_cache(maxsize=1)
def get_execution_backend() -> ExecutionBackend:
    """
    Get the execution backend instance.

    The backend is selected based on the `VALIDATOR_RUNNER` setting or
    auto-detected from the environment. The instance is cached as a singleton.

    Returns:
        ExecutionBackend instance.

    Raises:
        ValueError: If the configured backend is not registered.
    """
    # Ensure default backends are registered
    if not _BACKEND_CLASSES:
        _register_default_backends()

    backend_name = _get_backend_name()

    if backend_name not in _BACKEND_CLASSES:
        available = ", ".join(_BACKEND_CLASSES.keys())
        msg = (
            f"Unknown execution backend: {backend_name}. "
            f"Available backends: {available}"
        )
        raise ValueError(msg)

    backend_class = _BACKEND_CLASSES[backend_name]
    backend = backend_class()

    logger.info(
        "Initialized execution backend: %s (async=%s, available=%s)",
        backend.backend_name,
        backend.is_async,
        backend.is_available(),
    )

    return backend


def clear_backend_cache() -> None:
    """
    Clear the cached backend instance.

    Useful for testing or when settings change at runtime.
    """
    get_execution_backend.cache_clear()
