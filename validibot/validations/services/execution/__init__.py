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
   DockerComposeBackend   GCPBackend      AWSBackend
    (Celery+Docker)    (Cloud Run+GCS)  (SQS+ECS)
```

## Execution Models

Different backends have different execution characteristics:

- **Synchronous (Docker Compose)**: Worker blocks until container completes.
  Results are returned immediately via `execute()`.

- **Asynchronous (GCP)**: Job is triggered and returns immediately.
  Results arrive later via HTTP callback. `execute()` returns None.

## Usage

```python
from validibot.validations.services.execution import get_execution_backend

backend = get_execution_backend()

if backend.is_async:
    # Results will come via callback
    backend.execute(validator_type, input_envelope)
    step_run.status = "RUNNING"
else:
    # Results available immediately
    output = backend.execute(validator_type, input_envelope)
    process_output(output)
```

## Backend Selection

The backend is selected based on the `DEPLOYMENT_TARGET` setting:

- `"test"`, `"local_docker_compose"`, `"docker_compose"` → DockerComposeExecutionBackend

- `"gcp"` → GCPExecutionBackend (Cloud Run Jobs)
- `"aws"` → AWSBatchExecutionBackend (future)

The `VALIDATOR_RUNNER` setting can override this if needed.
"""

from validibot.validations.services.execution.base import ExecutionBackend
from validibot.validations.services.execution.registry import get_execution_backend

__all__ = [
    "ExecutionBackend",
    "get_execution_backend",
]
