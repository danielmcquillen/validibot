"""
Celery tasks for the core app.

This module serves as the entry point for Celery task autodiscovery.
Celery's autodiscover_tasks() looks for 'tasks.py' in each Django app,
so we import all task modules here to ensure they are registered.

Task modules:
    - scheduled_tasks: Periodic maintenance tasks (purge, cleanup, etc.)
    - validation_tasks: Validation execution tasks

For task definitions and scheduling details, see the individual modules.
"""

# Import all tasks so Celery's autodiscover finds them.
# Each module defines @shared_task decorated functions that get registered.

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
from validibot.core.tasks.validation_tasks import (  # noqa: F401
    execute_validation_run_task,
)
