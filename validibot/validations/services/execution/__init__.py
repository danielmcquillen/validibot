"""
Execution backends for advanced validators.

This module provides a unified interface for executing container-based validators
across different deployment targets. The key abstraction is `ExecutionBackend`,
which handles the full lifecycle of validation execution.

## Architecture

The execution layer sits between the validator and the infrastructure:

```
Validator (energyplus.py) → ExecutionBackend → Infrastructure
                              ↓
              ┌───────────────┼───────────────┐
              ↓               ↓               ↓
   DockerComposeBackend   Managed route adapter      AWSBackend
    (Celery+Docker)      (Service or retained Job)   (future)
```

## Execution Models

Different backends have different execution characteristics:

- **Synchronous (Docker Compose)**: Worker blocks until container completes.
  Results are returned immediately via `execute()`.

- **Asynchronous (GCP)**: The attempt pins either a request-driven Cloud Run
  Service or a retained Cloud Run Job. Dispatch returns a pending response and
  results arrive later via HTTP callback.

## Usage

Callers allocate an execution attempt first. Managed attempts pass the pinned
``ValidatorExecutionDeployment`` to ``get_execution_backend(deployment)``;
local and self-hosted callers use ``get_execution_backend()``.

## Backend Selection

Unmanaged backend selection is based on ``DEPLOYMENT_TARGET``:

- `"test"`, `"local_docker_compose"`, `"self_hosted"` → DockerComposeExecutionBackend
- `"aws"` → AWSBatchExecutionBackend (future)

On GCP, the attempt resolver pins an activated managed deployment before
provider contact. Its provider/deployment-kind pair then selects
``CloudRunServiceExecutionBackend`` or ``CloudRunJobsExecutionBackend``.
``VALIDATOR_RUNNER`` remains a local/legacy runner setting; it does not override
the adapter for a pinned managed attempt.
"""

from validibot.validations.services.execution.base import ExecutionBackend
from validibot.validations.services.execution.registry import get_execution_backend

__all__ = [
    "ExecutionBackend",
    "get_execution_backend",
]
