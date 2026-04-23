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

Cloud Tasks retries every non-2xx response. We therefore return:

* ``200`` for every *permanent* outcome the worker can reach:
  payload malformed (missing ``event_type``), referenced user / org
  / project deleted between enqueue and execution, unknown
  ``event_type`` string. Retrying won't change the outcome, so we
  deliberately acknowledge the task and log the drop reason.
* ``500`` for transient infrastructure failures — DB connection
  error, Redis down, etc. Cloud Tasks retries 5xx with exponential
  backoff.

The view calls :meth:`TrackingEventService._log_tracking_event`
(the raising path) rather than the public
:meth:`log_tracking_event` wrapper, because the wrapper catches
every exception and returns ``None``. That would silently
acknowledge transient failures as if they succeeded, defeating the
retry semantics we want from Cloud Tasks.
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
            # Permanently malformed — no amount of retrying recovers
            # a missing event_type. 200 deliberately acknowledges so
            # Cloud Tasks doesn't burn the retry budget on it.
            logger.warning(
                "LogTrackingEventView: payload missing event_type (dropped)",
            )
            return Response({"status": "dropped_missing_event_type"})

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
            # Deliberately use the private, raising method. The public
            # ``log_tracking_event`` wrapper swallows every exception
            # and returns ``None``, which would hide transient DB
            # failures from Cloud Tasks (it'd see a 200 and not
            # retry). Calling the raising path means a real DB error
            # bubbles to the ``except Exception`` below and becomes a
            # 500 that Cloud Tasks retries with backoff.
            service._log_tracking_event(
                event_type=event_type,
                app_event_type=app_event_type,
                project=project,
                org=org,
                user=user,
                extra_data=extra_data,
                channel=channel,
            )
            return Response({"status": "ok"})
        except ValueError as exc:
            # Raised by the service for structurally-bad payloads
            # (unknown event_type, missing required fields on the
            # service contract). Permanent failure — no retry.
            logger.warning(
                "LogTrackingEventView: permanently rejected event "
                "(event_type=%s app_event_type=%s): %s",
                event_type,
                app_event_type,
                exc,
            )
            return Response({"status": "dropped_invalid_payload"})

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
