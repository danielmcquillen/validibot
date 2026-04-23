"""
Celery tracking dispatcher — used on ``local_docker_compose`` and
``docker_compose``.

Thin wrapper around :func:`validibot.tracking.tasks.log_tracking_event_task`
— which has been the tracking write path since the module was added
and already implements retry, ack-late, and FK resolution. The
dispatcher's job is just to adapt the caller's
:class:`TrackingEventRequest` into kwargs the task accepts, and to
catch broker/connection errors so a dead Redis doesn't propagate
back up the auth-path critical section.

The broker-error catch is *inside* this dispatcher (rather than in
the signal receiver like the Stage 1 safety net) because that's
where the Celery-specific coupling lives. Other dispatchers
(inline, Cloud Tasks) don't have broker errors and don't need
that guard.
"""

from __future__ import annotations

import logging

from django.conf import settings

from validibot.tracking.dispatch.base import TrackingDispatcher
from validibot.tracking.dispatch.base import TrackingDispatchResponse
from validibot.tracking.dispatch.base import TrackingEventRequest

logger = logging.getLogger(__name__)


class CeleryTrackingDispatcher(TrackingDispatcher):
    """Enqueue tracking events on the project's Celery broker.

    Required settings:

    * ``CELERY_BROKER_URL`` — for ``.delay()`` to have somewhere to
      send the message.
    * ``django_celery_beat`` in ``INSTALLED_APPS`` — not strictly
      required for ``delay()``, but its presence is the signal that
      Celery is actually set up on this deployment (matches the
      validation-run dispatcher's availability check).
    """

    @property
    def dispatcher_name(self) -> str:
        return "celery"

    @property
    def is_sync(self) -> bool:
        return False

    def is_available(self) -> bool:
        if "django_celery_beat" not in settings.INSTALLED_APPS:
            return False
        return bool(getattr(settings, "CELERY_BROKER_URL", None))

    def dispatch(self, request: TrackingEventRequest) -> TrackingDispatchResponse:
        """Hand the tracking event off to Celery.

        Broker failures (Redis unreachable, connection refused,
        timeout) are caught and returned as a response with ``error``
        set. This is the failure mode that caused the prod 2FA 500
        before Stage 1 — we now handle it cleanly at the dispatcher
        boundary so the signal receiver doesn't need platform-aware
        safety nets.
        """
        # Local import: the signal module imports this dispatcher at
        # module load, and ``tracking.tasks`` pulls in Celery + the
        # service layer. Keeping the import inside ``dispatch`` means
        # Django's ``check`` / ``makemigrations`` phases can load the
        # signal module without requiring a fully-configured Celery.
        from validibot.tracking.tasks import log_tracking_event_task

        try:
            async_result = log_tracking_event_task.delay(
                event_type=request.event_type,
                app_event_type=request.app_event_type,
                user_id=request.user_id,
                org_id=request.org_id,
                project_id=request.project_id,
                extra_data=request.extra_data or None,
                channel=request.channel,
            )
        except Exception as exc:
            # Broad except is correct here: the whole purpose of this
            # catch is "never let a broker failure reach the auth
            # path." Narrowing to kombu / connection types risks
            # missing future Celery exception classes and reintroducing
            # the original outage. ``exc_info=True`` preserves the
            # traceback in Cloud Logging so the root cause is still
            # diagnosable.
            logger.warning(
                "Celery tracking dispatcher: failed to enqueue event "
                "(event_type=%s app_event_type=%s user_id=%s); "
                "event dropped",
                request.event_type,
                request.app_event_type,
                request.user_id,
                exc_info=True,
            )
            return TrackingDispatchResponse(
                task_id=None,
                is_sync=False,
                error=str(exc),
            )

        # ``async_result.id`` is the Celery task UUID. Surfacing it
        # lets callers correlate logs between the signal side and the
        # Celery worker side — useful when debugging
        # "why didn't that event land?"
        return TrackingDispatchResponse(
            task_id=getattr(async_result, "id", None),
            is_sync=False,
        )
