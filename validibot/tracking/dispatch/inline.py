"""
Inline (synchronous) tracking dispatcher for ``test`` and ``local_dev``.

Writes the tracking row in-process via
:class:`~validibot.tracking.services.TrackingEventService`, bypassing
Celery / Cloud Tasks entirely.

Why a dedicated dispatcher (rather than, say, ``CeleryDispatcher`` +
``CELERY_TASK_ALWAYS_EAGER=True`` as tests did before):

* Eliminates the implicit coupling where tests "happen to work"
  because Celery runs synchronously in eager mode. New tests don't
  need to know about Celery at all.
* Keeps the test dispatcher trivially fast — no task serialization,
  no broker connect attempts, no retry machinery.
* Gives ``local_dev`` (developer laptop, no broker) a working path
  that matches the tests' behaviour, so "works on my machine" and
  "works in CI" stay aligned.

Errors from the service layer *propagate* here, on purpose: a bug in
the tracking write is a bug in tests too, and swallowing it would
hide test failures. Production paths (Celery, Cloud Tasks) return
errors on the response object because they have real external
systems that can fail transiently; this one doesn't.
"""

from __future__ import annotations

import logging

from validibot.tracking.dispatch.base import TrackingDispatcher
from validibot.tracking.dispatch.base import TrackingDispatchResponse
from validibot.tracking.dispatch.base import TrackingEventRequest

logger = logging.getLogger(__name__)


class InlineTrackingDispatcher(TrackingDispatcher):
    """Synchronous, in-process tracking event writer.

    Selected for :class:`~validibot.core.constants.DeploymentTarget.TEST`
    and any future "no external broker available" target. Holds no
    state; one instance per process is fine.
    """

    @property
    def dispatcher_name(self) -> str:
        return "inline"

    @property
    def is_sync(self) -> bool:
        return True

    def is_available(self) -> bool:
        # Always — the dispatcher has no external dependencies.
        return True

    def dispatch(self, request: TrackingEventRequest) -> TrackingDispatchResponse:
        """Resolve FKs from PKs and call the service synchronously.

        Mirrors the FK-resolution logic in
        :func:`validibot.tracking.tasks.log_tracking_event_task` so
        tests exercise the same "user deleted between signal and
        task" code paths that run in production Celery workers.
        """
        # Imports are local so the module stays importable even when
        # the ORM hasn't been loaded (Django ``check`` phase, etc.).
        from validibot.projects.models import Project
        from validibot.tracking.services import TrackingEventService
        from validibot.users.models import Organization
        from validibot.users.models import User

        user = (
            User.objects.filter(pk=request.user_id).first() if request.user_id else None
        )
        org = (
            Organization.objects.filter(pk=request.org_id).first()
            if request.org_id
            else None
        )
        project = (
            Project.objects.filter(pk=request.project_id).first()
            if request.project_id
            else None
        )

        # user_id was supplied but the row no longer exists: log and
        # skip, mirroring the Celery task's behaviour. This shouldn't
        # fail the dispatch — the event is effectively orphaned.
        if request.user_id and user is None:
            logger.info(
                "Inline tracking dispatcher: user_id=%s no longer exists "
                "(event_type=%s app_event_type=%s); skipping",
                request.user_id,
                request.event_type,
                request.app_event_type,
            )
            return TrackingDispatchResponse(task_id=None, is_sync=True)

        service = TrackingEventService()
        service.log_tracking_event(
            event_type=request.event_type,
            app_event_type=request.app_event_type,
            project=project,
            org=org,
            user=user,
            extra_data=request.extra_data,
            channel=request.channel,
        )
        return TrackingDispatchResponse(task_id=None, is_sync=True)
