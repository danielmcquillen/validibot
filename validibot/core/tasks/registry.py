"""
Scheduled Task Registry - Single source of truth for periodic tasks.

This module provides a centralized registry of all scheduled/periodic tasks
that need to run in Validibot, regardless of the execution backend:

    - Celery Beat (Docker Compose deployments)
    - Google Cloud Scheduler (GCP deployments)
    - AWS EventBridge (future AWS deployments)

Each task is defined once here with all metadata needed for any backend:
    - Task identifier and human-readable name
    - Celery task path (for Docker Compose)
    - API endpoint (for cloud schedulers)
    - Schedule (cron expression and/or interval)
    - Description and enabled status

Usage:

    from validibot.core.tasks.registry import SCHEDULED_TASKS, get_tasks_for_backend

    # Get all tasks
    for task in SCHEDULED_TASKS:
        print(f"{task.name}: {task.schedule_cron}")

    # Get tasks for a specific backend
    celery_tasks = get_tasks_for_backend("celery")
    gcp_tasks = get_tasks_for_backend("gcp")

The registry is consumed by:
    - setup_validibot command (creates Celery Beat PeriodicTask records)
    - just gcp scheduler-setup (creates Cloud Scheduler jobs)
    - Management commands for schedule synchronization
"""

from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum


class ScheduleType(StrEnum):
    """Type of schedule for a task."""

    CRON = "cron"
    INTERVAL = "interval"


class Backend(StrEnum):
    """Execution backend for scheduled tasks."""

    CELERY = "celery"  # Docker Compose via Celery Beat
    GCP = "gcp"  # Google Cloud Scheduler
    AWS = "aws"  # AWS EventBridge (future)
    ALL = "all"  # Run on all backends


@dataclass(frozen=True)
class ScheduledTaskDefinition:
    """
    Definition of a scheduled task.

    This dataclass contains all information needed to register the task
    with any scheduling backend (Celery Beat, Cloud Scheduler, etc.).
    """

    # Identity
    id: str  # Unique identifier, e.g., "cleanup-stuck-runs"
    name: str  # Human-readable name for display

    # Celery configuration (Docker Compose)
    celery_task: str  # Full task path, e.g., "validibot.cleanup_stuck_runs"

    # Cloud Scheduler configuration (GCP)
    api_endpoint: str  # API path, e.g., "/api/v1/scheduled/cleanup-stuck-runs/"

    # Schedule - supports both cron and interval
    schedule_cron: str  # Cron expression, e.g., "*/10 * * * *"
    schedule_interval_minutes: int | None = None  # Alternative: interval in minutes

    # Metadata
    description: str = ""
    enabled: bool = True

    # Backend restrictions (default: run on all backends)
    backends: tuple[Backend, ...] = field(default=(Backend.ALL,))

    @property
    def job_name(self) -> str:
        """Generate Cloud Scheduler job name from task ID."""
        return f"validibot-{self.id}"

    def supports_backend(self, backend: Backend) -> bool:
        """Check if this task should run on the given backend."""
        if Backend.ALL in self.backends:
            return True
        return backend in self.backends


# =============================================================================
# SCHEDULED TASK DEFINITIONS
# =============================================================================
# This is the single source of truth for all scheduled tasks.
# Add new tasks here; they'll automatically be registered with all backends.

SCHEDULED_TASKS: tuple[ScheduledTaskDefinition, ...] = (
    # -------------------------------------------------------------------------
    # Submission/Output Purge Tasks
    # -------------------------------------------------------------------------
    ScheduledTaskDefinition(
        id="purge-expired-submissions",
        name="Purge Expired Submissions",
        celery_task="validibot.purge_expired_submissions",
        api_endpoint="/api/v1/scheduled/purge-expired-submissions/",
        schedule_cron="0 * * * *",  # Hourly at :00
        description="Remove submission content past retention period",
    ),
    ScheduledTaskDefinition(
        id="purge-expired-outputs",
        name="Purge Expired Outputs",
        celery_task="validibot.purge_expired_outputs",
        api_endpoint="/api/v1/scheduled/purge-expired-outputs/",
        schedule_cron="0 * * * *",  # Hourly at :00
        description="Remove validation output content past retention period",
        # Note: GCP endpoint not yet implemented, Celery-only for now
        backends=(Backend.CELERY,),
    ),
    ScheduledTaskDefinition(
        id="process-purge-retries",
        name="Process Purge Retries",
        celery_task="validibot.process_purge_retries",
        api_endpoint="/api/v1/scheduled/process-purge-retries/",
        schedule_cron="*/5 * * * *",  # Every 5 minutes
        schedule_interval_minutes=5,
        description="Retry failed purge operations",
    ),
    # -------------------------------------------------------------------------
    # Run Cleanup Tasks
    # -------------------------------------------------------------------------
    ScheduledTaskDefinition(
        id="cleanup-stuck-runs",
        name="Cleanup Stuck Runs",
        celery_task="validibot.cleanup_stuck_runs",
        api_endpoint="/api/v1/scheduled/cleanup-stuck-runs/",
        schedule_cron="*/10 * * * *",  # Every 10 minutes
        schedule_interval_minutes=10,
        description="Mark validation runs stuck >30min as FAILED",
    ),
    # -------------------------------------------------------------------------
    # API Cleanup Tasks
    # -------------------------------------------------------------------------
    ScheduledTaskDefinition(
        id="cleanup-idempotency-keys",
        name="Cleanup Idempotency Keys",
        celery_task="validibot.cleanup_idempotency_keys",
        api_endpoint="/api/v1/scheduled/cleanup-idempotency-keys/",
        schedule_cron="0 3 * * *",  # Daily at 3:00 AM
        description="Remove expired API idempotency keys (24h TTL)",
    ),
    ScheduledTaskDefinition(
        id="cleanup-callback-receipts",
        name="Cleanup Callback Receipts",
        celery_task="validibot.cleanup_callback_receipts",
        api_endpoint="/api/v1/scheduled/cleanup-callback-receipts/",
        schedule_cron="0 4 * * 0",  # Weekly on Sunday at 4:00 AM
        description="Delete old validator callback receipts (30-day retention)",
    ),
    # -------------------------------------------------------------------------
    # Session Management
    # -------------------------------------------------------------------------
    ScheduledTaskDefinition(
        id="clear-sessions",
        name="Clear Sessions",
        celery_task="validibot.clear_sessions",
        api_endpoint="/api/v1/scheduled/clear-sessions/",
        schedule_cron="0 2 * * *",  # Daily at 2:00 AM
        description="Clear expired Django sessions",
    ),
    # -------------------------------------------------------------------------
    # Docker Compose Only Tasks
    # -------------------------------------------------------------------------
    ScheduledTaskDefinition(
        id="cleanup-orphaned-containers",
        name="Cleanup Orphaned Containers",
        celery_task="validibot.cleanup_orphaned_containers",
        api_endpoint="/api/v1/scheduled/cleanup-orphaned-containers/",
        schedule_cron="*/10 * * * *",  # Every 10 minutes
        schedule_interval_minutes=10,
        description="Remove orphaned Docker validator containers (Docker Compose only)",
        backends=(Backend.CELERY,),  # Only for Docker Compose deployments
    ),
)


def get_tasks_for_backend(backend: Backend | str) -> list[ScheduledTaskDefinition]:
    """
    Get all tasks that should run on the specified backend.

    Args:
        backend: The backend to filter for (Backend enum or string)

    Returns:
        List of task definitions for that backend
    """
    if isinstance(backend, str):
        backend = Backend(backend)

    return [task for task in SCHEDULED_TASKS if task.supports_backend(backend)]


def get_task_by_id(task_id: str) -> ScheduledTaskDefinition | None:
    """
    Get a task definition by its ID.

    Args:
        task_id: The task identifier (e.g., "cleanup-stuck-runs")

    Returns:
        The task definition, or None if not found
    """
    for task in SCHEDULED_TASKS:
        if task.id == task_id:
            return task
    return None


def get_enabled_tasks() -> list[ScheduledTaskDefinition]:
    """Get all enabled task definitions."""
    return [task for task in SCHEDULED_TASKS if task.enabled]
