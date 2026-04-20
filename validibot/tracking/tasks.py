"""
Celery tasks for asynchronous tracking-event writes.

The tracking ``TrackingEvent`` writes used to happen inline in
``user_logged_in`` / ``user_logged_out`` signal receivers — every
login blocked on the insert. If the tracking table ever lives on a
slow WAL-backed disk or a separate replica, that latency lands on
the auth-path critical path.

This task accepts primitive arguments only (no model instances).
Celery serializes task arguments into the broker; passing a model
instance serializes a stale copy that may not match the DB state
when the worker dequeues. PK-based re-resolution is the
conventional safe pattern. See refactor-step item ``[review-#11]``.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import shared_task
from django.db import OperationalError

from validibot.events.constants import AppEventType
from validibot.tracking.constants import TrackingEventType
from validibot.tracking.services import TrackingEventService

logger = logging.getLogger(__name__)


# Transient failures worth retrying — same set the community
# ``scheduled_tasks`` module uses for its management-command
# wrappers. Non-transient failures (bad event_type, deleted user)
# should not retry.
_RETRYABLE = (
    OperationalError,
    ConnectionError,
    TimeoutError,
    OSError,
)


@shared_task(
    bind=True,
    name="validibot.log_tracking_event",
    autoretry_for=_RETRYABLE,
    max_retries=3,
    retry_backoff=30,
    retry_backoff_max=300,
    acks_late=True,
)
def log_tracking_event_task(
    self,
    *,
    event_type: str,
    app_event_type: str | None = None,
    user_id: int | None = None,
    org_id: int | None = None,
    project_id: int | None = None,
    extra_data: dict[str, Any] | None = None,
    channel: str | None = None,
) -> None:
    """Resolve FKs from PKs and write a tracking event.

    Non-retryable failures (e.g. the user was deleted between the
    signal firing and the task running) are swallowed with a warning
    log. The auth event is already complete from the user's
    perspective; failing the Celery task would just retry the same
    impossible write. ``TrackingEventService.log_tracking_event``
    itself already catches and logs exceptions inside the write
    path — this task is a thin dispatcher on top.
    """
    from validibot.projects.models import Project
    from validibot.users.models import Organization
    from validibot.users.models import User

    user = User.objects.filter(pk=user_id).first() if user_id else None
    org = Organization.objects.filter(pk=org_id).first() if org_id else None
    project = Project.objects.filter(pk=project_id).first() if project_id else None

    if user_id and user is None:
        logger.info(
            "tracking event skipped: user_id=%s no longer exists "
            "(event_type=%s app_event_type=%s)",
            user_id,
            event_type,
            app_event_type,
        )
        return

    service = TrackingEventService()
    service.log_tracking_event(
        event_type=event_type,
        app_event_type=app_event_type,
        project=project,
        org=org,
        user=user,
        extra_data=extra_data,
        channel=channel,
    )


# Expose the typed choices at task-module level so signal receivers
# don't need their own ``from validibot.events.constants import ...``
# lines — keeps the "enqueue a tracking event" surface in one place.
__all__ = [
    "AppEventType",
    "TrackingEventType",
    "log_tracking_event_task",
]
