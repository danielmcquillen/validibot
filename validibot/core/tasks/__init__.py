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

## Admin Task Registry

The admin task registry (`registry.py`) is the authoritative source for all
scheduled admin task definitions. Read it via the accessor functions —
never by importing `SCHEDULED_ADMIN_TASKS` directly, because that tuple only
contains community-owned static tasks. Downstream packages (cloud, pro,
enterprise) register their own tasks dynamically at `AppConfig.ready()`
time, and only the accessors see both halves.

```python
from validibot.core.tasks.registry import (
    get_all_admin_tasks,
    get_admin_tasks_for_backend,
)

# Every task, static + dynamic
all_tasks = get_all_admin_tasks()

# Tasks filtered to one backend (Celery Beat, GCP Cloud Scheduler, ...)
celery_tasks = get_admin_tasks_for_backend("celery")
gcp_tasks = get_admin_tasks_for_backend("gcp")
```

### Read-path contract

- **Core / community tasks** live in the static `SCHEDULED_ADMIN_TASKS`
  tuple in `registry.py`. Plain data, easy to reason about, no import-order
  surprises.
- **Extension tasks** from downstream packages register via
  `register_scheduled_admin_task(...)` in their `AppConfig.ready()`.
- **Consumers** (sync tools, API endpoints, admin commands) must read
  through `get_all_admin_tasks()` or `get_admin_tasks_for_backend(...)`.
  Direct reads of `SCHEDULED_ADMIN_TASKS` are a bug — they miss extension
  tasks.
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
# ``SCHEDULED_ADMIN_TASKS`` is deliberately NOT re-exported here — consumers
# should read through the accessor functions below so both community-static
# tasks and downstream-registered dynamic tasks are visible. See the
# read-path contract in this module's docstring.
from validibot.core.tasks.registry import Backend
from validibot.core.tasks.registry import ScheduledAdminTaskDefinition
from validibot.core.tasks.registry import get_admin_task_by_id
from validibot.core.tasks.registry import get_admin_tasks_for_backend
from validibot.core.tasks.registry import get_all_admin_tasks
from validibot.core.tasks.registry import get_enabled_admin_tasks
from validibot.core.tasks.registry import register_scheduled_admin_task
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
    "Backend",
    "ScheduledAdminTaskDefinition",
    "enqueue_validation_run",
    "get_admin_task_by_id",
    "get_admin_tasks_for_backend",
    "get_all_admin_tasks",
    "get_enabled_admin_tasks",
    "register_scheduled_admin_task",
]
