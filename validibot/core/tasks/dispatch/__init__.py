"""
Task dispatch for validation run execution.

This module provides a unified interface for dispatching validation run tasks
across different deployment targets. The key abstraction is `TaskDispatcher`,
which handles enqueueing tasks to the appropriate backend.

## Architecture

The dispatch layer sits between the API and the task queue:

```
API (views.py) → get_task_dispatcher() → TaskDispatcher
                                              ↓
                    ┌─────────────────────────┼─────────────────────────┐
                    ↓                         ↓                         ↓
            TestDispatcher          DramatiqDispatcher          GoogleCloudTasksDispatcher
            (sync inline)           (Redis queue)               (GCP Cloud Tasks)
```

## Dispatchers

Different dispatchers have different execution characteristics:

- **Test (test)**: Synchronous inline execution. No external dependencies.
- **Local Dev (local_dev)**: HTTP call to worker. Requires worker running.
- **Dramatiq (dramatiq)**: Redis-backed queue. Self-hosted production.
- **Cloud Tasks (cloud_tasks)**: GCP Cloud Tasks queue. GCP production.

## Usage

```python
from validibot.core.tasks.dispatch import (
    get_task_dispatcher,
    TaskDispatchRequest,
)

dispatcher = get_task_dispatcher()
response = dispatcher.dispatch(TaskDispatchRequest(
    validation_run_id=run.id,
    user_id=user.id,
))

if response.error:
    logger.error("Task dispatch failed: %s", response.error)
elif response.is_sync:
    logger.info("Task completed synchronously")
else:
    logger.info("Task queued with ID: %s", response.task_id)
```

## Dispatcher Selection

The dispatcher is selected based on the `DEPLOYMENT_TARGET` setting:

- `"test"` → TestDispatcher (synchronous inline)
- `"local_docker_compose"` → LocalDevDispatcher (HTTP to worker)
- `"docker_compose"` → DramatiqDispatcher (Redis queue)
- `"gcp"` → GoogleCloudTasksDispatcher (Cloud Tasks)
- `"aws"` → Not yet implemented

The DEPLOYMENT_TARGET setting is required and must be set in your settings file.
"""

from validibot.core.tasks.dispatch.base import TaskDispatcher
from validibot.core.tasks.dispatch.base import TaskDispatchRequest
from validibot.core.tasks.dispatch.base import TaskDispatchResponse
from validibot.core.tasks.dispatch.registry import clear_dispatcher_cache
from validibot.core.tasks.dispatch.registry import get_task_dispatcher

__all__ = [
    "TaskDispatchRequest",
    "TaskDispatchResponse",
    "TaskDispatcher",
    "clear_dispatcher_cache",
    "get_task_dispatcher",
]
