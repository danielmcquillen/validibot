"""
Task execution for Validibot.

This module provides task execution mechanisms that vary by deployment target:

1. **Test**: Synchronous inline execution (no task queue)

2. **Local development**: Direct HTTP calls to worker service

3. **Docker Compose**:
   - Celery with Redis broker for task queuing
   - Celery Beat for scheduled task triggering
   - Workers process tasks from the queue

4. **Google Cloud**:
   - Cloud Tasks for async job queuing
   - Delivers tasks to Cloud Run worker via HTTP
   - Cloud Scheduler for periodic tasks

5. **AWS**: TBD (not yet implemented)

## Task Dispatching

Use the `enqueue_validation_run` function to dispatch validation tasks:

```python
from validibot.core.tasks import enqueue_validation_run

enqueue_validation_run(
    validation_run_id=run.id,
    user_id=user.id,
)
```

The function automatically selects the appropriate dispatcher based on
environment configuration. See `validibot.core.tasks.dispatch` for the
underlying dispatcher architecture.

## Celery Tasks

Celery tasks are defined in submodules and imported here for autodiscovery:
- `scheduled_tasks.py`: Periodic maintenance tasks (purge, cleanup, etc.)
- `validation_tasks.py`: Validation execution task

Celery's `autodiscover_tasks()` looks for `tasks.py` (or `tasks/__init__.py`)
in each Django app. By importing the tasks here, they get registered.

## Task Registry

The task registry (`registry.py`) is the single source of truth for all
scheduled task definitions. It provides metadata for both Celery Beat
and Cloud Scheduler backends:

```python
from validibot.core.tasks.registry import SCHEDULED_TASKS, get_tasks_for_backend

# Get all tasks for Celery Beat
celery_tasks = get_tasks_for_backend("celery")

# Get all tasks for GCP Cloud Scheduler
gcp_tasks = get_tasks_for_backend("gcp")
```
"""

# =============================================================================
# CELERY TASK IMPORTS
# =============================================================================
# Import all Celery tasks so they are registered when this module is loaded.
# Celery's autodiscover_tasks() finds this module (validibot.core.tasks)
# and imports it, which triggers these imports.

# Scheduled maintenance tasks (periodic tasks run by Celery Beat)
# =============================================================================
# PUBLIC API
# =============================================================================
from validibot.core.tasks.registry import SCHEDULED_TASKS
from validibot.core.tasks.registry import Backend
from validibot.core.tasks.registry import ScheduledTaskDefinition
from validibot.core.tasks.registry import get_enabled_tasks
from validibot.core.tasks.registry import get_task_by_id
from validibot.core.tasks.registry import get_tasks_for_backend
from validibot.core.tasks.scheduled_tasks import cleanup_callback_receipts  # noqa: F401
from validibot.core.tasks.scheduled_tasks import cleanup_idempotency_keys  # noqa: F401
from validibot.core.tasks.scheduled_tasks import (  # noqa: F401
    cleanup_orphaned_containers,
)
from validibot.core.tasks.scheduled_tasks import cleanup_stuck_runs  # noqa: F401
from validibot.core.tasks.scheduled_tasks import clear_sessions  # noqa: F401
from validibot.core.tasks.scheduled_tasks import process_purge_retries  # noqa: F401
from validibot.core.tasks.scheduled_tasks import purge_expired_outputs  # noqa: F401
from validibot.core.tasks.scheduled_tasks import purge_expired_submissions  # noqa: F401
from validibot.core.tasks.task_dispatch import enqueue_validation_run

# Validation execution task (dispatched by CeleryDispatcher)
from validibot.core.tasks.validation_tasks import (  # noqa: F401
    execute_validation_run_task,
)

__all__ = [
    "SCHEDULED_TASKS",
    "Backend",
    "ScheduledTaskDefinition",
    "enqueue_validation_run",
    "get_enabled_tasks",
    "get_task_by_id",
    "get_tasks_for_backend",
]
