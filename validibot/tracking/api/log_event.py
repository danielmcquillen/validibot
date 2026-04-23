"""
Worker endpoint that writes a tracking event from a Cloud Task.

Called by :class:`validibot.tracking.dispatch.cloud_tasks.CloudTasksTrackingDispatcher`
with the JSON payload produced by :meth:`TrackingEventRequest.to_payload`.
The endpoint resolves FKs from PKs, calls the tracking service, and
returns ``200 OK`` so Cloud Tasks considers the task complete.

Security
--------

Lives behind :class:`~validibot.core.api.worker.WorkerOnlyAPIView`,
which enforces three things:

1. Only serves on worker instances (``APP_IS_WORKER=True``). A probe
   from the public web surface gets a 404 before auth runs.
2. Delegates authentication to
   :func:`~validibot.core.api.task_auth.get_worker_auth_classes`, which
   picks the right backend for the current ``DEPLOYMENT_TARGET``:

   * ``gcp`` → Cloud Run IAM + application-layer OIDC verification
     (:class:`CloudTasksOIDCAuthentication`).
   * ``docker_compose`` → shared-secret ``WORKER_API_KEY`` header.

3. On GCP, Cloud Run IAM is the primary control; OIDC verification
   is defence in depth against IAM misconfiguration.

Error handling
--------------

* ``400`` if the payload is missing ``event_type`` — the task is
  malformed and retrying won't help. Cloud Tasks treats 4xx as a
  permanent failure.
* ``200`` when the event is recorded OR when the referenced user /
  org / project has been deleted between enqueue and execution. The
  tracking row is orphaned; retrying won't recover it.
* ``500`` for DB connection errors and other transient infrastructure
  failures. Cloud Tasks retries 5xx with exponential backoff.
"""

from __future__ import annotations

import logging

from rest_framework import status
from rest_framework.response import Response

from validibot.core.api.worker import WorkerOnlyAPIView

logger = logging.getLogger(__name__)


class LogTrackingEventView(WorkerOnlyAPIView):
    """Cloud Tasks → worker: persist a single tracking event.

    URL (wired in ``config/api_internal_router.py``):
    ``POST /api/v1/tasks/tracking/log-event/``
    """

    def post(self, request):
        """Record the tracking event encoded in ``request.data``.

        Request body matches :meth:`TrackingEventRequest.to_payload`::

            {
                "event_type": "APP_EVENT",
                "app_event_type": "USER_LOGGED_IN",
                "user_id": 42,
                "org_id": 7,
                "project_id": null,
                "extra_data": {"user_agent": "...", "path": "/accounts/..."},
                "channel": "web"
            }
        """
        # Imports are local so unit tests of the router / view config
        # don't pay for the tracking service's import-time cost.
        from validibot.projects.models import Project
        from validibot.tracking.services import TrackingEventService
        from validibot.users.models import Organization
        from validibot.users.models import User

        event_type = request.data.get("event_type")
        if not event_type:
            logger.warning(
                "LogTrackingEventView: payload missing event_type (dropped)",
            )
            return Response(
                {"error": "event_type is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        app_event_type = request.data.get("app_event_type")
        user_id = request.data.get("user_id")
        org_id = request.data.get("org_id")
        project_id = request.data.get("project_id")
        extra_data = request.data.get("extra_data")
        channel = request.data.get("channel")

        try:
            # FK resolution happens inside the try block because a
            # deleted user is a known-orphaned-event case we want to
            # log and 200, not propagate as a 500 that Cloud Tasks
            # would retry pointlessly.
            user = User.objects.filter(pk=user_id).first() if user_id else None
            org = Organization.objects.filter(pk=org_id).first() if org_id else None
            project = (
                Project.objects.filter(pk=project_id).first() if project_id else None
            )

            if user_id and user is None:
                # Orphaned: user was deleted between the signal and
                # this worker call. Log, 200, move on. Matches the
                # Celery task's behaviour so the two paths stay
                # semantically identical.
                logger.info(
                    "LogTrackingEventView: user_id=%s no longer exists "
                    "(event_type=%s app_event_type=%s); skipping",
                    user_id,
                    event_type,
                    app_event_type,
                )
                return Response({"status": "skipped_missing_user"})

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
            return Response({"status": "ok"})

        except Exception as exc:
            # Transient infrastructure failure — DB connection, etc.
            # Return 500 so Cloud Tasks retries with backoff. The
            # service itself is tolerant of malformed event_type /
            # app_event_type values (logs and skips), so anything
            # that escapes to here is genuinely a problem worth
            # retrying.
            logger.exception(
                "LogTrackingEventView: unexpected failure writing tracking event "
                "(event_type=%s app_event_type=%s user_id=%s)",
                event_type,
                app_event_type,
                user_id,
            )
            return Response(
                {"error": str(exc)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
