"""
Dramatiq task dispatcher for self-hosted deployments.

Enqueues validation tasks to a Dramatiq queue with Redis broker.
Workers running `dramatiq validibot.core.tasks.actors` process the tasks.

This is the primary dispatcher for Docker Compose self-hosted deployments.
"""

from __future__ import annotations

import logging

import dramatiq
from django.conf import settings

from validibot.core.tasks.dispatch.base import TaskDispatcher
from validibot.core.tasks.dispatch.base import TaskDispatchRequest
from validibot.core.tasks.dispatch.base import TaskDispatchResponse

logger = logging.getLogger(__name__)


# Define the Dramatiq actor for validation run execution
# This is imported and used by the worker process
@dramatiq.actor(queue_name="validation_runs", max_retries=3)
def execute_validation_run_task(
    validation_run_id: str,
    user_id: int,
    resume_from_step: int | None = None,
) -> None:
    """
    Dramatiq actor to execute a validation run.

    This actor is picked up by Dramatiq workers and executes the validation
    workflow. It wraps ValidationRunService.execute_workflow_steps().

    Args:
        validation_run_id: ID of the ValidationRun to execute.
        user_id: ID of the user who initiated the run.
        resume_from_step: Step order to resume from (None for initial execution).
    """
    from validibot.validations.services.validation_run import ValidationRunService

    logger.info(
        "Dramatiq actor: executing validation_run_id=%s user_id=%s resume_from_step=%s",
        validation_run_id,
        user_id,
        resume_from_step,
    )

    service = ValidationRunService()
    service.execute_workflow_steps(
        validation_run_id=validation_run_id,
        user_id=user_id,
        metadata=None,
        resume_from_step=resume_from_step,
    )

    logger.info(
        "Dramatiq actor: completed validation_run_id=%s",
        validation_run_id,
    )


class DramatiqDispatcher(TaskDispatcher):
    """
    Dramatiq dispatcher - async task queue with Redis broker.

    Enqueues validation tasks to the Dramatiq broker (typically Redis).
    Workers process tasks from the queue asynchronously.

    This is the primary dispatcher for self-hosted Docker Compose deployments.

    Required settings:
    - DRAMATIQ_BROKER must be configured (handled by django_dramatiq)
    """

    @property
    def dispatcher_name(self) -> str:
        return "dramatiq"

    @property
    def is_sync(self) -> bool:
        return False

    def is_available(self) -> bool:
        """Check if Dramatiq broker is configured."""
        # Check if django_dramatiq is in INSTALLED_APPS
        return "django_dramatiq" in settings.INSTALLED_APPS

    def dispatch(self, request: TaskDispatchRequest) -> TaskDispatchResponse:
        """Enqueue task via Dramatiq."""
        if not self.is_available():
            return TaskDispatchResponse(
                task_id=None,
                is_sync=False,
                error="Dramatiq broker not configured (django_dramatiq not installed)",
            )

        logger.info(
            "Dramatiq dispatcher: enqueueing validation_run_id=%s user_id=%s",
            request.validation_run_id,
            request.user_id,
        )

        try:
            # Send the message to the broker
            message = execute_validation_run_task.send(
                validation_run_id=str(request.validation_run_id),
                user_id=request.user_id,
                resume_from_step=request.resume_from_step,
            )

            logger.info(
                "Dramatiq dispatcher: enqueued message_id=%s for validation_run_id=%s",
                message.message_id,
                request.validation_run_id,
            )

            return TaskDispatchResponse(
                task_id=message.message_id,
                is_sync=False,
            )

        except Exception as exc:
            logger.exception(
                "Dramatiq dispatcher: failed to enqueue validation_run_id=%s",
                request.validation_run_id,
            )
            return TaskDispatchResponse(
                task_id=None,
                is_sync=False,
                error=str(exc),
            )
