"""
Task execution for Validibot.

This module provides task execution mechanisms that vary by deployment target:

1. **Test**: Synchronous inline execution (no task queue)

2. **Local development**: Direct HTTP calls to worker service

3. **Self-hosted (Docker Compose)**:
   - Dramatiq with Redis broker for task queuing
   - Periodiq for scheduled task triggering
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

## Scheduled Tasks

For scheduled tasks (session cleanup, expired data purge, etc.),
see `scheduled_actors.py` which defines periodic Dramatiq actors.
"""

from validibot.core.tasks.task_dispatch import enqueue_validation_run

__all__ = ["enqueue_validation_run"]
