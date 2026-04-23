"""
Tracking event dispatch — pick the right async backend per deployment target.

Signal receivers in ``validibot.tracking.signals`` (and any future
caller that needs to record a tracking event out-of-band) ask this
package for a dispatcher and hand it a
:class:`TrackingEventRequest`. The dispatcher encapsulates the
mechanism for getting that event onto whichever task queue / executor
the current deployment uses:

* ``test`` / ``local_dev`` → :class:`InlineTrackingDispatcher` (synchronous,
  calls :class:`~validibot.tracking.services.TrackingEventService` directly)
* ``local_docker_compose`` / ``docker_compose`` →
  :class:`CeleryTrackingDispatcher` (enqueues via Redis broker)
* ``gcp`` → :class:`CloudTasksTrackingDispatcher` (enqueues via Cloud
  Tasks queue; the worker receives at
  ``/api/v1/tasks/tracking/log-event/``)

The shape mirrors the validation-run dispatch package at
``validibot.core.tasks.dispatch`` — same ABC, same registry + lru_cache
pattern, same "errors never propagate; return a response with
``error`` set" contract. Keeping the pattern identical means the next
domain that needs async work on GCP can follow the same template.

Usage::

    from validibot.tracking.dispatch import (
        TrackingEventRequest,
        get_tracking_dispatcher,
    )

    dispatcher = get_tracking_dispatcher()
    response = dispatcher.dispatch(TrackingEventRequest(
        event_type=TrackingEventType.APP_EVENT,
        app_event_type=AppEventType.USER_LOGGED_IN,
        user_id=user.pk,
        org_id=org.pk if org else None,
        extra_data={"user_agent": ua},
        channel="web",
    ))

    if response.error:
        logger.warning("Tracking dispatch failed: %s", response.error)

``transaction.on_commit`` ordering (so the event isn't enqueued for a
rollback-bound transaction) stays a caller concern — see
:func:`validibot.tracking.signals._enqueue_tracking_event`.
"""

from validibot.tracking.dispatch.base import TrackingDispatcher
from validibot.tracking.dispatch.base import TrackingDispatchResponse
from validibot.tracking.dispatch.base import TrackingEventRequest
from validibot.tracking.dispatch.registry import clear_tracking_dispatcher_cache
from validibot.tracking.dispatch.registry import get_tracking_dispatcher

__all__ = [
    "TrackingDispatchResponse",
    "TrackingDispatcher",
    "TrackingEventRequest",
    "clear_tracking_dispatcher_cache",
    "get_tracking_dispatcher",
]
