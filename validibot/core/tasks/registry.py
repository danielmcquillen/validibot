"""
Scheduled Admin Task Registry - Single source of truth for periodic admin tasks.

This module provides a centralized registry of all scheduled admin/maintenance
tasks that need to run in Validibot, regardless of the execution backend:

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

    from validibot.core.tasks.registry import (
        get_all_admin_tasks,
        get_admin_tasks_for_backend,
    )

    # Every task — static built-ins AND runtime-registered
    # extension tasks from cloud / pro / enterprise.
    for task in get_all_admin_tasks():
        print(f"{task.name}: {task.schedule_cron}")

    # Tasks filtered to a specific backend.
    celery_tasks = get_admin_tasks_for_backend("celery")
    gcp_tasks = get_admin_tasks_for_backend("gcp")

### Read-path contract

Two write paths, ONE read path:

- **Core / community built-ins** live in the static
  ``SCHEDULED_ADMIN_TASKS`` tuple below. Plain data, easy to read.
- **Extension tasks** from downstream packages
  (``validibot-cloud``, ``validibot-pro``, ``validibot-enterprise``)
  register dynamically via :func:`register_scheduled_admin_task`
  at ``AppConfig.ready()`` time.
- **Consumers MUST read through the accessor functions**
  (:func:`get_all_admin_tasks`, :func:`get_admin_tasks_for_backend`,
  :func:`get_admin_task_by_id`, :func:`get_enabled_admin_tasks`).

Reading ``SCHEDULED_ADMIN_TASKS`` directly from outside this
module is a bug — it sees only half the registry and will silently
omit every dynamically-registered extension task. See
``tests/test_registry_read_path.py`` for the test that enforces
this.

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
class ScheduledAdminTaskDefinition:
    """
    Definition of a scheduled admin/maintenance task.

    These are system housekeeping tasks (purging, cleanup, session management)
    — not user-triggered validation tasks. This dataclass contains all
    information needed to register the task with any scheduling backend
    (Celery Beat, Cloud Scheduler, etc.).
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
# SCHEDULED ADMIN TASK DEFINITIONS
# =============================================================================
# This is the single source of truth for all scheduled admin tasks.
# Add new tasks here; they'll automatically be registered with all backends.

SCHEDULED_ADMIN_TASKS: tuple[ScheduledAdminTaskDefinition, ...] = (
    # -------------------------------------------------------------------------
    # Submission/Output Purge Tasks
    # -------------------------------------------------------------------------
    ScheduledAdminTaskDefinition(
        id="purge-expired-submissions",
        name="Purge Expired Submissions",
        celery_task="validibot.purge_expired_submissions",
        api_endpoint="/api/v1/scheduled/purge-expired-submissions/",
        schedule_cron="0 * * * *",  # Hourly at :00
        description="Remove submission content past retention period",
    ),
    ScheduledAdminTaskDefinition(
        id="purge-expired-outputs",
        name="Purge Expired Outputs",
        celery_task="validibot.purge_expired_outputs",
        api_endpoint="/api/v1/scheduled/purge-expired-outputs/",
        schedule_cron="0 * * * *",  # Hourly at :00
        description="Remove validation output content past retention period",
    ),
    ScheduledAdminTaskDefinition(
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
    ScheduledAdminTaskDefinition(
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
    ScheduledAdminTaskDefinition(
        id="cleanup-idempotency-keys",
        name="Cleanup Idempotency Keys",
        celery_task="validibot.cleanup_idempotency_keys",
        api_endpoint="/api/v1/scheduled/cleanup-idempotency-keys/",
        schedule_cron="0 3 * * *",  # Daily at 3:00 AM
        description="Remove expired API idempotency keys (24h TTL)",
    ),
    ScheduledAdminTaskDefinition(
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
    ScheduledAdminTaskDefinition(
        id="clear-sessions",
        name="Clear Sessions",
        celery_task="validibot.clear_sessions",
        api_endpoint="/api/v1/scheduled/clear-sessions/",
        schedule_cron="0 2 * * *",  # Daily at 2:00 AM
        description="Clear expired Django sessions",
    ),
    # -------------------------------------------------------------------------
    # Periodic Emails
    # -------------------------------------------------------------------------
    ScheduledAdminTaskDefinition(
        id="send-periodic-emails",
        name="Send Periodic Emails",
        celery_task="validibot.send_periodic_emails",
        api_endpoint="/api/v1/scheduled/send-periodic-emails/",
        schedule_cron="0 */6 * * *",  # Every 6 hours
        description=(
            "Run registered periodic email handlers. No-op in community-only installs."
        ),
    ),
    # -------------------------------------------------------------------------
    # Docker Compose Only Tasks
    # -------------------------------------------------------------------------
    ScheduledAdminTaskDefinition(
        id="cleanup-orphaned-containers",
        name="Cleanup Orphaned Containers",
        celery_task="validibot.cleanup_orphaned_containers",
        api_endpoint="/api/v1/scheduled/cleanup-orphaned-containers/",
        schedule_cron="*/10 * * * *",  # Every 10 minutes
        schedule_interval_minutes=10,
        description="Remove orphaned Docker validator containers (Docker Compose only)",
        backends=(Backend.CELERY,),  # Only for Docker Compose deployments
    ),
    # -------------------------------------------------------------------------
    # Audit log retention
    # -------------------------------------------------------------------------
    ScheduledAdminTaskDefinition(
        id="enforce-audit-retention",
        name="Enforce Audit Log Retention",
        celery_task="validibot.enforce_audit_retention",
        api_endpoint="/api/v1/scheduled/enforce-audit-retention/",
        schedule_cron="30 2 * * *",  # Daily at 02:30 server time
        description=(
            "Delete AuditLogEntry rows older than AUDIT_HOT_RETENTION_DAYS. "
            "Calls AUDIT_ARCHIVE_BACKEND.archive() first so Pro/Enterprise/"
            "cloud deployments preserve the rows before deletion. Community "
            "default (NullArchiveBackend) discards them without preserving."
        ),
    ),
)


# =============================================================================
# RUNTIME-REGISTERED TASKS
# =============================================================================
# Extension point for downstream packages (``validibot-cloud``,
# ``validibot-pro``, ``validibot-enterprise``) to contribute their own
# scheduled tasks. Community code never knows about these — a cloud-only
# concern like "verify license-document hashes against stored
# acceptances" has no business being in the public-repo registry.
#
# Downstream packages call ``register_scheduled_admin_task(...)`` from
# their ``AppConfig.ready()``, mirroring how commercial packages call
# ``validibot.core.license.set_license(...)``. The registration hook is
# process-local and import-time; it has no runtime cost once Django
# has booted.

_DYNAMIC_TASKS: list[ScheduledAdminTaskDefinition] = []


def register_scheduled_admin_task(task: ScheduledAdminTaskDefinition) -> None:
    """Register a scheduled admin task from a downstream package.

    The shared ``sync_schedules`` machinery (Celery Beat + Cloud
    Scheduler) reads the union of ``SCHEDULED_ADMIN_TASKS`` and the
    dynamically-registered tasks via :func:`get_all_admin_tasks`.

    Duplicate registration (same ``id``) is an error — it almost
    certainly means two AppConfig.ready bodies are registering the
    same task, which would duplicate the scheduled row.
    """
    for existing in _DYNAMIC_TASKS:
        if existing.id == task.id:
            msg = f"Scheduled admin task already registered: {task.id!r}"
            raise ValueError(msg)
    _DYNAMIC_TASKS.append(task)


def get_all_admin_tasks() -> tuple[ScheduledAdminTaskDefinition, ...]:
    """Return static + dynamically-registered admin tasks as one tuple.

    Used by the sync machinery and any tooling that needs the full
    picture. Order is stable: static tasks first (in declaration
    order), then dynamic tasks in registration order.
    """
    return SCHEDULED_ADMIN_TASKS + tuple(_DYNAMIC_TASKS)


def reset_dynamic_tasks() -> None:
    """Clear runtime-registered tasks (for testing)."""
    _DYNAMIC_TASKS.clear()


def get_admin_tasks_for_backend(
    backend: Backend | str,
) -> list[ScheduledAdminTaskDefinition]:
    """
    Get all tasks that should run on the specified backend.

    Includes both the static ``SCHEDULED_ADMIN_TASKS`` and any
    downstream-registered tasks.

    Args:
        backend: The backend to filter for (Backend enum or string)

    Returns:
        List of task definitions for that backend
    """
    if isinstance(backend, str):
        backend = Backend(backend)

    return [task for task in get_all_admin_tasks() if task.supports_backend(backend)]


def get_admin_task_by_id(task_id: str) -> ScheduledAdminTaskDefinition | None:
    """
    Get a task definition by its ID.

    Args:
        task_id: The task identifier (e.g., "cleanup-stuck-runs")

    Returns:
        The task definition, or None if not found
    """
    for task in get_all_admin_tasks():
        if task.id == task_id:
            return task
    return None


def get_enabled_admin_tasks() -> list[ScheduledAdminTaskDefinition]:
    """Get all enabled admin task definitions (static + dynamic)."""
    return [task for task in get_all_admin_tasks() if task.enabled]
