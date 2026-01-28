"""
Dramatiq actors for scheduled tasks.

This module defines periodic tasks that run on schedules using periodiq.
These replace the previous Cloud Scheduler + HTTP endpoint approach with
a self-contained solution that works in any Docker deployment.

Architecture:
    1. Scheduler (periodiq) triggers tasks on schedule
    2. Broker (Redis) queues the task messages
    3. Worker (dramatiq) processes the tasks

Each actor wraps a Django management command, providing:
    - Scheduled execution via cron expressions
    - Automatic retries on failure
    - Admin visibility via django_dramatiq
    - Logging and error reporting

To run the scheduler:
    periodiq validibot.core.tasks.scheduled_actors

To run the worker:
    dramatiq validibot.core.tasks.scheduled_actors

See docs/dev_docs/how-to/configure-scheduled-tasks.md for details.
"""

import logging
from datetime import UTC
from datetime import datetime
from io import StringIO

import dramatiq
from django.core.management import call_command
from periodiq import cron

logger = logging.getLogger(__name__)


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
    """
    out = StringIO()
    err = StringIO() if capture_stderr else None

    try:
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

    except Exception as e:
        logger.exception("Management command failed: %s", command_name)
        result = {
            "status": "failed",
            "command": command_name,
            "error": str(e),
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }

    return result


# =============================================================================
# SCHEDULED TASKS
# =============================================================================
# Cron expression format: minute hour day month day_of_week
#
# Examples:
#   "0 * * * *"     - Every hour at :00
#   "*/5 * * * *"   - Every 5 minutes
#   "*/10 * * * *"  - Every 10 minutes
#   "0 2 * * *"     - Daily at 2:00 AM
#   "0 3 * * *"     - Daily at 3:00 AM
#   "0 4 * * 0"     - Weekly on Sunday at 4:00 AM


@dramatiq.actor(periodic=cron("0 * * * *"))  # Hourly at :00
def purge_expired_submissions() -> dict:
    """
    Purge submission content that has passed its retention period.

    Removes content from submissions where expires_at < now while
    preserving the submission record for audit purposes.

    Schedule: Hourly
    """
    logger.info("Starting scheduled purge of expired submissions")

    result = _run_management_command(
        "purge_expired_submissions",
        "--batch-size=100",
        "--max-batches=10",
        capture_stderr=True,
    )

    if result["status"] == "completed":
        logger.info("Expired submission purge completed: %s", result.get("output", ""))
    else:
        logger.error("Expired submission purge failed: %s", result.get("error", ""))

    return result


@dramatiq.actor(periodic=cron("0 * * * *"))  # Hourly at :00
def purge_expired_outputs() -> dict:
    """
    Purge validation outputs that have passed their retention period.

    Removes findings, artifacts, and storage files from runs where
    output_expires_at < now while preserving the run record for audit.

    Schedule: Hourly
    """
    logger.info("Starting scheduled purge of expired outputs")

    result = _run_management_command(
        "purge_expired_outputs",
        "--batch-size=100",
        "--max-batches=10",
        capture_stderr=True,
    )

    if result["status"] == "completed":
        logger.info("Expired output purge completed: %s", result.get("output", ""))
    else:
        logger.error("Expired output purge failed: %s", result.get("error", ""))

    return result


@dramatiq.actor(periodic=cron("*/5 * * * *"))  # Every 5 minutes
def process_purge_retries() -> dict:
    """
    Process failed submission purge retries.

    Retries purging submissions that failed on previous attempts
    (e.g., due to storage unavailability).

    Schedule: Every 5 minutes
    """
    logger.info("Starting scheduled processing of purge retries")

    result = _run_management_command(
        "process_purge_retries",
        "--batch-size=50",
        capture_stderr=True,
    )

    if result["status"] == "completed":
        logger.info("Purge retry processing completed: %s", result.get("output", ""))
    else:
        logger.error("Purge retry processing failed: %s", result.get("error", ""))

    return result


@dramatiq.actor(periodic=cron("*/10 * * * *"))  # Every 10 minutes
def cleanup_stuck_runs() -> dict:
    """
    Mark stuck validation runs as FAILED.

    Validation runs can become "stuck" in RUNNING status if a validator
    container crashes without completing, or if processing hangs. This
    watchdog finds runs that have been RUNNING longer than a threshold
    and marks them as FAILED.

    Schedule: Every 10 minutes
    """
    logger.info("Starting scheduled cleanup of stuck runs")

    result = _run_management_command(
        "cleanup_stuck_runs",
        "--timeout-minutes=30",
        "--batch-size=100",
    )

    if result["status"] == "completed":
        logger.info("Stuck run cleanup completed: %s", result.get("output", ""))
    else:
        logger.error("Stuck run cleanup failed: %s", result.get("error", ""))

    return result


@dramatiq.actor(periodic=cron("0 3 * * *"))  # Daily at 3:00 AM
def cleanup_idempotency_keys() -> dict:
    """
    Clean up expired idempotency keys.

    Idempotency keys are used to prevent duplicate API requests.
    They expire after 24 hours and can be safely removed.

    Schedule: Daily at 3:00 AM
    """
    logger.info("Starting scheduled cleanup of idempotency keys")

    result = _run_management_command("cleanup_idempotency_keys")

    if result["status"] == "completed":
        logger.info("Idempotency key cleanup completed: %s", result.get("output", ""))
    else:
        logger.error("Idempotency key cleanup failed: %s", result.get("error", ""))

    return result


@dramatiq.actor(periodic=cron("0 4 * * 0"))  # Weekly on Sunday at 4:00 AM
def cleanup_callback_receipts() -> dict:
    """
    Clean up old callback receipts.

    Callback receipts are used for idempotency when processing validator
    callbacks. Old receipts (default: 30 days) can be safely deleted.

    Schedule: Weekly on Sunday at 4:00 AM
    """
    logger.info("Starting scheduled cleanup of callback receipts")

    result = _run_management_command(
        "cleanup_callback_receipts",
        "--days=30",
    )

    if result["status"] == "completed":
        logger.info("Callback receipt cleanup completed: %s", result.get("output", ""))
    else:
        logger.error("Callback receipt cleanup failed: %s", result.get("error", ""))

    return result


@dramatiq.actor(periodic=cron("0 2 * * *"))  # Daily at 2:00 AM
def clear_sessions() -> dict:
    """
    Clear expired Django sessions.

    Django's clearsessions command removes expired sessions from the
    database. This helps keep the database clean and performant.

    Schedule: Daily at 2:00 AM
    """
    logger.info("Starting scheduled session cleanup")

    result = _run_management_command("clearsessions")

    # clearsessions doesn't output anything on success
    if not result.get("output"):
        result["output"] = "Sessions cleared successfully"

    if result["status"] == "completed":
        logger.info("Session cleanup completed")
    else:
        logger.error("Session cleanup failed: %s", result.get("error", ""))

    return result
