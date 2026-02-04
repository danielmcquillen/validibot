"""
Celery task dispatcher for Docker Compose deployments.

Enqueues validation tasks to a Celery queue with Redis broker.
Workers running `celery -A config worker` process the tasks.

This is the primary dispatcher for Docker Compose production deployments.
"""

from __future__ import annotations

import logging
import uuid

from celery import current_app
from celery import shared_task
from django.conf import settings
from django.db import OperationalError
from django.db import transaction
from django.utils import timezone

from validibot.core.tasks.dispatch.base import TaskDispatcher
from validibot.core.tasks.dispatch.base import TaskDispatchRequest
from validibot.core.tasks.dispatch.base import TaskDispatchResponse

logger = logging.getLogger(__name__)

# Exceptions that indicate transient failures worth retrying.
# Other exceptions (programming errors, business logic failures) should not retry.
RETRYABLE_EXCEPTIONS = (
    OperationalError,  # Database connection issues
    ConnectionError,  # Network issues
    TimeoutError,  # Timeouts
    OSError,  # File system / network issues
)

# Generic error message shown to users when a task-level failure occurs.
TASK_FAILURE_ERROR = (
    "A system error prevented the validation from completing. "
    "Please try again in a few minutes."
)


@shared_task(
    bind=True,
    name="validibot.execute_validation_run",
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def execute_validation_run_task(
    self,
    validation_run_id: str,
    user_id: int,
    resume_from_step: int | None = None,
) -> None:
    """
    Celery task to execute a validation run.

    This task is picked up by Celery workers and executes the validation
    workflow. It wraps ValidationRunService.execute_workflow_steps().

    Args:
        self: Celery task instance (bound task).
        validation_run_id: ID of the ValidationRun to execute.
        user_id: ID of the user who initiated the run.
        resume_from_step: Step order to resume from (None for initial execution).
    """
    from validibot.validations.services.validation_run import ValidationRunService

    logger.info(
        "Celery task: executing validation_run_id=%s user_id=%s "
        "resume_from_step=%s task_id=%s",
        validation_run_id,
        user_id,
        resume_from_step,
        self.request.id,
    )

    try:
        service = ValidationRunService()
        service.execute_workflow_steps(
            validation_run_id=validation_run_id,
            user_id=user_id,
            metadata=None,
            resume_from_step=resume_from_step,
        )

        logger.info(
            "Celery task: completed validation_run_id=%s task_id=%s",
            validation_run_id,
            self.request.id,
        )
    except RETRYABLE_EXCEPTIONS as exc:
        # Transient errors - retry with exponential backoff
        logger.exception(
            "Celery task: transient error (will retry) validation_run_id=%s "
            "task_id=%s retry=%s/%s error_type=%s",
            validation_run_id,
            self.request.id,
            self.request.retries,
            self.max_retries,
            type(exc).__name__,
        )
        raise self.retry(exc=exc) from exc
    except Exception as exc:
        # Permanent errors - mark ValidationRun as FAILED and don't retry.
        # This ensures users see a final status even if the error occurred
        # outside of execute_workflow_steps (e.g., ValidationRun not found).
        logger.exception(
            "Celery task: permanent failure (no retry) validation_run_id=%s "
            "task_id=%s error_type=%s",
            validation_run_id,
            self.request.id,
            type(exc).__name__,
        )
        _mark_validation_run_failed(
            validation_run_id=validation_run_id,
            error_message=TASK_FAILURE_ERROR,
            exception=exc,
        )
        # Re-raise so Celery marks the task as failed
        raise


def _mark_validation_run_failed(
    validation_run_id: str,
    error_message: str,
    exception: Exception,
) -> None:
    """
    Mark a ValidationRun as FAILED due to a task-level error.

    This is called when an error occurs outside of ValidationRunService
    (e.g., ValidationRun not found, database errors). It ensures the user
    sees a final FAILED status rather than being stuck in PENDING/RUNNING.

    This function catches all exceptions to ensure the calling code can
    still re-raise the original exception.
    """
    from validibot.validations.constants import ValidationRunErrorCategory
    from validibot.validations.constants import ValidationRunStatus
    from validibot.validations.models import ValidationRun

    try:
        run = ValidationRun.objects.filter(id=validation_run_id).first()
        if not run:
            logger.warning(
                "Cannot mark ValidationRun as failed - not found: %s",
                validation_run_id,
            )
            return

        # Only update if not already in a terminal state
        if run.status in (
            ValidationRunStatus.SUCCEEDED,
            ValidationRunStatus.FAILED,
            ValidationRunStatus.CANCELED,
        ):
            logger.info(
                "ValidationRun already in terminal state %s, not updating: %s",
                run.status,
                validation_run_id,
            )
            return

        run.status = ValidationRunStatus.FAILED
        run.error = error_message
        run.error_category = ValidationRunErrorCategory.SYSTEM_ERROR
        run.ended_at = timezone.now()
        run.save(update_fields=["status", "error", "error_category", "ended_at"])

        logger.info(
            "Marked ValidationRun as FAILED due to task error: %s",
            validation_run_id,
        )
    except Exception:
        # Don't let this helper cause additional failures - just log
        logger.exception(
            "Failed to mark ValidationRun as FAILED: %s",
            validation_run_id,
        )


class CeleryDispatcher(TaskDispatcher):
    """
    Celery dispatcher - async task queue with Redis broker.

    Enqueues validation tasks to the Celery broker (Redis).
    Workers process tasks from the queue asynchronously.

    This is the primary dispatcher for Docker Compose production deployments.

    Required settings:
    - CELERY_BROKER_URL must be configured
    - django_celery_beat must be in INSTALLED_APPS (for periodic tasks)
    """

    @property
    def dispatcher_name(self) -> str:
        return "celery"

    @property
    def is_sync(self) -> bool:
        return False

    def is_available(self) -> bool:
        """Check if Celery is configured and broker is reachable."""
        # Check if django_celery_beat is in INSTALLED_APPS
        if "django_celery_beat" not in settings.INSTALLED_APPS:
            return False

        # Check if broker URL is configured
        return bool(getattr(settings, "CELERY_BROKER_URL", None))

    def dispatch(self, request: TaskDispatchRequest) -> TaskDispatchResponse:
        """Enqueue task via Celery."""
        if not self.is_available():
            return TaskDispatchResponse(
                task_id=None,
                is_sync=False,
                error=(
                    "Celery not configured "
                    "(django_celery_beat not installed or broker not set)"
                ),
            )

        logger.info(
            "Celery dispatcher: enqueueing validation_run_id=%s user_id=%s",
            request.validation_run_id,
            request.user_id,
        )

        try:
            # Use delay_on_commit to ensure the task is only sent after the
            # current database transaction commits. This prevents race conditions
            # where the worker tries to fetch a ValidationRun that doesn't exist yet.
            # Requires Celery 5.4+
            #
            # If not in a transaction (autocommit mode), this behaves like .delay()
            task_kwargs = {
                "validation_run_id": str(request.validation_run_id),
                "user_id": request.user_id,
                "resume_from_step": request.resume_from_step,
            }

            # Generate a deterministic task ID upfront so we can return it
            # even when deferring task dispatch until transaction commit.
            # This allows callers to track the task regardless of timing.
            task_id = f"task-{request.validation_run_id}-{uuid.uuid4().hex[:8]}"

            # Check if we're in eager mode (tests)
            if current_app.conf.task_always_eager:
                # In eager mode, delay_on_commit doesn't work properly,
                # so use regular delay
                execute_validation_run_task.apply_async(
                    kwargs=task_kwargs,
                    task_id=task_id,
                )
            elif transaction.get_connection().in_atomic_block:
                # We're in a transaction - use on_commit to defer sending.
                # We pass the pre-generated task_id so the returned ID is valid.
                def send_task():
                    execute_validation_run_task.apply_async(
                        kwargs=task_kwargs,
                        task_id=task_id,
                    )

                transaction.on_commit(send_task)

                logger.info(
                    "Celery dispatcher: task deferred until transaction commit "
                    "task_id=%s validation_run_id=%s",
                    task_id,
                    request.validation_run_id,
                )

                return TaskDispatchResponse(
                    task_id=task_id,
                    is_sync=False,
                )
            else:
                # Not in a transaction - send immediately
                execute_validation_run_task.apply_async(
                    kwargs=task_kwargs,
                    task_id=task_id,
                )

            logger.info(
                "Celery dispatcher: enqueued task_id=%s for validation_run_id=%s",
                task_id,
                request.validation_run_id,
            )

            return TaskDispatchResponse(
                task_id=task_id,
                is_sync=False,
            )

        except Exception as exc:
            logger.exception(
                "Celery dispatcher: failed to enqueue validation_run_id=%s",
                request.validation_run_id,
            )
            return TaskDispatchResponse(
                task_id=None,
                is_sync=False,
                error=str(exc),
            )
