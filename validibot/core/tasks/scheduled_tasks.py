"""
Celery tasks for scheduled maintenance operations.

This module defines periodic tasks that run on schedules using Celery Beat.
These are used in self-hosted deployments where Celery + Redis handles
task queuing and scheduling.

Architecture (Self-hosted / Docker Compose):
    1. Beat scheduler triggers tasks on schedule (DatabaseScheduler)
    2. Broker (Redis) queues the task messages
    3. Worker (celery worker) processes the tasks

For Google Cloud deployments, scheduled tasks are handled by Cloud Scheduler
triggering HTTP endpoints instead of Celery tasks.

Task scheduling is configured via:
    1. Django admin (Periodic Tasks UI provided by django-celery-beat)
    2. Data migration (see core/migrations/XXXX_celery_beat_schedules.py)

Each task wraps a Django management command, providing:
    - Scheduled execution via cron or interval schedules
    - Automatic retries on transient failures (DB/network issues)
    - Logging and error reporting

To run the worker:
    celery -A config worker --loglevel=info

To run the beat scheduler:
    celery -A config beat --loglevel=info \\
        --scheduler django_celery_beat.schedulers:DatabaseScheduler

See docs/dev_docs/how-to/configure-scheduled-tasks.md for details.
"""

import logging
from datetime import UTC
from datetime import datetime
from io import StringIO

from celery import shared_task
from django.core.management import call_command
from django.db import OperationalError

logger = logging.getLogger(__name__)

# Exceptions that indicate transient failures worth retrying.
# These are used by autoretry_for on scheduled tasks.
RETRYABLE_EXCEPTIONS = (
    OperationalError,  # Database connection issues
    ConnectionError,  # Network issues
    TimeoutError,  # Timeouts
    OSError,  # File system / network issues
)


def _run_management_command(
    command_name: str,
    *args: str,
    capture_stderr: bool = False,
) -> dict:
    """
    Run a Django management command and return results.

    Args:
        command_name: Name of the management command to run
        *args: Command line arguments to pass
        capture_stderr: Whether to capture stderr output

    Returns:
        Dict with status, output, and optional errors

    Raises:
        Exception: Re-raises any exception from the management command.
            This allows Celery's autoretry_for to handle retryable errors.
    """
    out = StringIO()
    err = StringIO() if capture_stderr else None

    # Let exceptions propagate so Celery's autoretry_for can handle them
    if capture_stderr:
        call_command(command_name, *args, stdout=out, stderr=err)
    else:
        call_command(command_name, *args, stdout=out)

    result = {
        "status": "completed",
        "command": command_name,
        "output": out.getvalue().strip(),
        "timestamp": datetime.now(tz=UTC).isoformat(),
    }

    if capture_stderr and err:
        errors = err.getvalue().strip()
        if errors:
            result["errors"] = errors

    return result


# =============================================================================
# SCHEDULED TASKS
# =============================================================================
# Schedule configuration is stored in the django_celery_beat database tables.
# The schedules below are set up via data migration.
#
# Default schedules:
#   purge_expired_submissions     - Hourly at :00
#   purge_expired_outputs         - Hourly at :00
#   process_purge_retries         - Every 5 minutes
#   cleanup_stuck_runs            - Every 10 minutes
#   cleanup_idempotency_keys      - Daily at 3:00 AM
#   cleanup_callback_receipts     - Weekly on Sunday at 4:00 AM
#   clear_sessions                - Daily at 2:00 AM
#   cleanup_orphaned_containers   - Every 10 minutes (self-hosted only)


@shared_task(
    bind=True,
    name="validibot.purge_expired_submissions",
    autoretry_for=RETRYABLE_EXCEPTIONS,
    max_retries=3,
    retry_backoff=60,  # Exponential backoff starting at 60s
    retry_backoff_max=600,  # Max 10 minutes between retries
    acks_late=True,
)
def purge_expired_submissions(self) -> dict:
    """
    Purge submission content that has passed its retention period.

    Removes content from submissions where expires_at < now while
    preserving the submission record for audit purposes.

    Default schedule: Hourly at :00
    """
    logger.info(
        "Starting scheduled purge of expired submissions (task_id=%s)",
        self.request.id,
    )

    result = _run_management_command(
        "purge_expired_submissions",
        "--batch-size=100",
        "--max-batches=10",
        capture_stderr=True,
    )

    logger.info("Expired submission purge completed: %s", result.get("output", ""))
    return result


@shared_task(
    bind=True,
    name="validibot.purge_expired_outputs",
    autoretry_for=RETRYABLE_EXCEPTIONS,
    max_retries=3,
    retry_backoff=60,
    retry_backoff_max=600,
    acks_late=True,
)
def purge_expired_outputs(self) -> dict:
    """
    Purge validation outputs that have passed their retention period.

    Removes findings, artifacts, and storage files from runs where
    output_expires_at < now while preserving the run record for audit.

    Default schedule: Hourly at :00
    """
    logger.info(
        "Starting scheduled purge of expired outputs (task_id=%s)",
        self.request.id,
    )

    result = _run_management_command(
        "purge_expired_outputs",
        "--batch-size=100",
        "--max-batches=10",
        capture_stderr=True,
    )

    logger.info("Expired output purge completed: %s", result.get("output", ""))

    return result


@shared_task(
    bind=True,
    name="validibot.process_purge_retries",
    autoretry_for=RETRYABLE_EXCEPTIONS,
    max_retries=3,
    retry_backoff=30,
    retry_backoff_max=300,
    acks_late=True,
)
def process_purge_retries(self) -> dict:
    """
    Process failed submission purge retries.

    Retries purging submissions that failed on previous attempts
    (e.g., due to storage unavailability).

    Default schedule: Every 5 minutes
    """
    logger.info(
        "Starting scheduled processing of purge retries (task_id=%s)",
        self.request.id,
    )

    result = _run_management_command(
        "process_purge_retries",
        "--batch-size=50",
        capture_stderr=True,
    )

    logger.info("Purge retry processing completed: %s", result.get("output", ""))
    return result


@shared_task(
    bind=True,
    name="validibot.cleanup_stuck_runs",
    autoretry_for=RETRYABLE_EXCEPTIONS,
    max_retries=3,
    retry_backoff=60,
    retry_backoff_max=300,
    acks_late=True,
)
def cleanup_stuck_runs(self) -> dict:
    """
    Mark stuck validation runs as FAILED.

    Validation runs can become "stuck" in RUNNING status if a validator
    container crashes without completing, or if processing hangs. This
    watchdog finds runs that have been RUNNING longer than a threshold
    and marks them as FAILED.

    Default schedule: Every 10 minutes
    """
    logger.info(
        "Starting scheduled cleanup of stuck runs (task_id=%s)",
        self.request.id,
    )

    result = _run_management_command(
        "cleanup_stuck_runs",
        "--timeout-minutes=30",
        "--batch-size=100",
    )

    logger.info("Stuck run cleanup completed: %s", result.get("output", ""))
    return result


@shared_task(
    bind=True,
    name="validibot.cleanup_idempotency_keys",
    autoretry_for=RETRYABLE_EXCEPTIONS,
    max_retries=3,
    retry_backoff=60,
    retry_backoff_max=600,
    acks_late=True,
)
def cleanup_idempotency_keys(self) -> dict:
    """
    Clean up expired idempotency keys.

    Idempotency keys are used to prevent duplicate API requests.
    They expire after 24 hours and can be safely removed.

    Default schedule: Daily at 3:00 AM
    """
    logger.info(
        "Starting scheduled cleanup of idempotency keys (task_id=%s)",
        self.request.id,
    )

    result = _run_management_command("cleanup_idempotency_keys")

    logger.info("Idempotency key cleanup completed: %s", result.get("output", ""))
    return result


@shared_task(
    bind=True,
    name="validibot.cleanup_callback_receipts",
    autoretry_for=RETRYABLE_EXCEPTIONS,
    max_retries=3,
    retry_backoff=60,
    retry_backoff_max=600,
    acks_late=True,
)
def cleanup_callback_receipts(self) -> dict:
    """
    Clean up old callback receipts.

    Callback receipts are used for idempotency when processing validator
    callbacks. Old receipts (default: 30 days) can be safely deleted.

    Default schedule: Weekly on Sunday at 4:00 AM
    """
    logger.info(
        "Starting scheduled cleanup of callback receipts (task_id=%s)",
        self.request.id,
    )

    result = _run_management_command(
        "cleanup_callback_receipts",
        "--days=30",
    )

    logger.info("Callback receipt cleanup completed: %s", result.get("output", ""))
    return result


@shared_task(
    bind=True,
    name="validibot.clear_sessions",
    autoretry_for=RETRYABLE_EXCEPTIONS,
    max_retries=3,
    retry_backoff=60,
    retry_backoff_max=600,
    acks_late=True,
)
def clear_sessions(self) -> dict:
    """
    Clear expired Django sessions.

    Django's clearsessions command removes expired sessions from the
    database. This helps keep the database clean and performant.

    Default schedule: Daily at 2:00 AM
    """
    logger.info("Starting scheduled session cleanup (task_id=%s)", self.request.id)

    result = _run_management_command("clearsessions")

    # clearsessions doesn't output anything on success
    if not result.get("output"):
        result["output"] = "Sessions cleared successfully"

    logger.info("Session cleanup completed")
    return result


@shared_task(
    bind=True,
    name="validibot.cleanup_orphaned_containers",
    autoretry_for=RETRYABLE_EXCEPTIONS,
    max_retries=3,
    retry_backoff=60,
    retry_backoff_max=300,
    acks_late=True,
)
def cleanup_orphaned_containers(self) -> dict:
    """
    Clean up orphaned Docker containers.

    Removes Validibot-managed containers that have exceeded their timeout
    plus a grace period (5 minutes). This handles cases where a worker
    crashed while running a validator container.

    This task only runs on self-hosted deployments (Docker Compose).
    On GCP deployments, container cleanup is handled by Cloud Run.

    Default schedule: Every 10 minutes
    """
    from django.conf import settings

    # Only run on self-hosted deployments where Docker containers are used.
    # GCP deployments use Cloud Run which handles its own container lifecycle.
    deployment_target = getattr(settings, "DEPLOYMENT_TARGET", "")
    if deployment_target not in ("docker_compose", "local_docker_compose"):
        logger.debug(
            "Skipping container cleanup on deployment target: %s",
            deployment_target,
        )
        return {
            "status": "skipped",
            "reason": f"Not applicable for deployment target: {deployment_target}",
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }

    logger.info(
        "Starting scheduled cleanup of orphaned containers (task_id=%s)",
        self.request.id,
    )

    result = _run_management_command(
        "cleanup_containers",
        "--grace-period=300",  # 5 minutes grace period
    )

    logger.info("Orphaned container cleanup completed: %s", result.get("output", ""))
    return result
