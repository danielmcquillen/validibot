"""
Google Cloud Tasks tracking dispatcher — used on ``gcp``.

Creates a Cloud Task that POSTs the event payload to a worker-only
endpoint on the Cloud Run worker service. The worker verifies the
inbound OIDC token (same defence-in-depth pattern as
``/api/v1/execute-validation-run/`` and the ``/api/v1/scheduled/*``
endpoints) and hands the payload to
:class:`~validibot.tracking.services.TrackingEventService`.

Why Cloud Tasks, not Pub/Sub or direct HTTP:

* Cloud Tasks is the project's existing "retry this HTTP call with
  backoff" primitive, already used by the validation-run dispatcher.
  Reusing it keeps the operator's mental model uniform (one queue,
  one retry policy, one place to look for stuck tasks).
* Pub/Sub would require a separate subscription and push endpoint,
  for what is still a unicast "fire this at the worker" use case.
* Direct HTTP from the web instance to the worker would lose the
  retry / dead-letter guarantees; a slow tracking write would
  block the auth request.

Mirrors the validation-run
:class:`~validibot.core.tasks.dispatch.google_cloud_tasks.GoogleCloudTasksDispatcher`
closely — same queue, same OIDC auth, same settings — differing only
in the target URL and payload shape.
"""

from __future__ import annotations

import json
import logging

from django.conf import settings

from validibot.tracking.dispatch.base import TrackingDispatcher
from validibot.tracking.dispatch.base import TrackingDispatchResponse
from validibot.tracking.dispatch.base import TrackingEventRequest

logger = logging.getLogger(__name__)


# Endpoint path the Cloud Task will POST to on the worker service.
# Defined as a module-level constant so the worker-side URL config
# can import it and stay in lockstep with the dispatcher — if the
# path drifts, both sides fail to build rather than one silently
# posting to a 404.
WORKER_ENDPOINT_PATH = "/api/v1/tasks/tracking/log-event/"


class CloudTasksTrackingDispatcher(TrackingDispatcher):
    """Enqueue tracking events on Google Cloud Tasks.

    Required settings (mirrors
    :class:`GoogleCloudTasksDispatcher`):

    * ``GCP_PROJECT_ID`` — target project.
    * ``GCS_TASK_QUEUE_NAME`` — Cloud Tasks queue name. The tracking
      events reuse the validation queue rather than standing up a
      separate one. Volume is low (one per login/logout) and queue
      fan-out is an optimisation worth deferring until we see
      contention.
    * ``WORKER_URL`` — origin the worker service is reachable at.
    * ``CLOUD_TASKS_SERVICE_ACCOUNT`` — SA whose OIDC token the task
      will be signed with. Must have ``roles/run.invoker`` on the
      worker.

    Optional:

    * ``GCP_REGION`` — defaults to ``us-west1`` to match the existing
      dispatcher; override via settings for other regions.
    """

    @property
    def dispatcher_name(self) -> str:
        return "cloud_tasks"

    @property
    def is_sync(self) -> bool:
        return False

    def is_available(self) -> bool:
        return all(
            [
                getattr(settings, "GCP_PROJECT_ID", None),
                getattr(settings, "GCS_TASK_QUEUE_NAME", None),
                getattr(settings, "WORKER_URL", None),
                getattr(settings, "CLOUD_TASKS_SERVICE_ACCOUNT", None),
            ],
        )

    def dispatch(self, request: TrackingEventRequest) -> TrackingDispatchResponse:
        """Submit the event as a Cloud Task HTTP POST.

        Returns a response with ``error`` set for any dispatch
        failure — missing config, client error, API rejection. Does
        not raise. The signal receiver catches any leaked exception
        as a last-resort safety net but shouldn't see one in normal
        operation.
        """
        # Config check up front. A missing setting is a deploy-time
        # error the operator should fix, but we still return a
        # response rather than raising — the auth request must not
        # fail because the tracking wiring is incomplete.
        project_id = getattr(settings, "GCP_PROJECT_ID", "")
        queue_name = getattr(settings, "GCS_TASK_QUEUE_NAME", "")
        worker_url = getattr(settings, "WORKER_URL", "")
        service_account = getattr(settings, "CLOUD_TASKS_SERVICE_ACCOUNT", "")
        if not all([project_id, queue_name, worker_url, service_account]):
            msg = (
                "Cloud Tasks tracking dispatcher misconfigured: need "
                "GCP_PROJECT_ID, GCS_TASK_QUEUE_NAME, WORKER_URL, and "
                "CLOUD_TASKS_SERVICE_ACCOUNT."
            )
            logger.error(msg)
            return TrackingDispatchResponse(
                task_id=None,
                is_sync=False,
                error=msg,
            )

        region = getattr(settings, "GCP_REGION", "us-west1")
        queue_path = f"projects/{project_id}/locations/{region}/queues/{queue_name}"
        endpoint_url = f"{worker_url.rstrip('/')}{WORKER_ENDPOINT_PATH}"

        # Local import: google-cloud-tasks brings in gRPC and auth
        # machinery we don't want at module-load time on the web
        # instance (slows startup, costs memory on every gunicorn
        # worker). Only imported when a task is actually being
        # dispatched, i.e. when we know GCP is in use.
        try:
            from google.cloud import tasks_v2
        except ImportError as exc:
            msg = f"google-cloud-tasks not installed: {exc}"
            logger.exception(msg)
            return TrackingDispatchResponse(
                task_id=None,
                is_sync=False,
                error=msg,
            )

        payload = request.to_payload()
        task = tasks_v2.Task(
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=endpoint_url,
                headers={"Content-Type": "application/json"},
                body=json.dumps(payload).encode(),
                # OIDC token carries the service account's identity.
                # The worker verifies it with CloudTasksOIDCAuthentication,
                # which checks signature + audience + allowed SA list.
                # Audience MUST match the worker origin exactly — Cloud
                # Tasks and the worker's verification settings both
                # use the WORKER_URL scheme+host.
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=service_account,
                    audience=worker_url,
                ),
            ),
        )
        create_request = tasks_v2.CreateTaskRequest(parent=queue_path, task=task)

        try:
            client = tasks_v2.CloudTasksClient()
            created = client.create_task(request=create_request)
        except Exception as exc:
            # Same reasoning as the Celery dispatcher's broad catch:
            # this layer exists to absorb any transport-level failure
            # so the auth path continues. Log with exc_info so the
            # root cause is visible in Cloud Logging.
            logger.warning(
                "Cloud Tasks tracking dispatcher: failed to create task "
                "(event_type=%s app_event_type=%s user_id=%s); event dropped",
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

        logger.info(
            "Cloud Tasks tracking dispatcher: enqueued task=%s "
            "event_type=%s app_event_type=%s user_id=%s",
            created.name,
            request.event_type,
            request.app_event_type,
            request.user_id,
        )
        return TrackingDispatchResponse(
            task_id=created.name,
            is_sync=False,
        )
