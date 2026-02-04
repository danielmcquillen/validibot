"""
Google Cloud Tasks dispatcher.

Enqueues validation tasks to a Cloud Tasks queue, which delivers them to
the Cloud Run worker service via HTTP with OIDC authentication.
"""

from __future__ import annotations

import json
import logging

from django.conf import settings

from validibot.core.tasks.dispatch.base import TaskDispatcher
from validibot.core.tasks.dispatch.base import TaskDispatchRequest
from validibot.core.tasks.dispatch.base import TaskDispatchResponse

logger = logging.getLogger(__name__)


class GoogleCloudTasksDispatcher(TaskDispatcher):
    """
    Google Cloud Tasks dispatcher - async task queue.

    Creates tasks that POST to the worker's execute-validation-run endpoint
    with OIDC authentication. The worker is typically a Cloud Run service.

    Required settings:
    - GCP_PROJECT_ID: Google Cloud project ID
    - GCS_TASK_QUEUE_NAME: Cloud Tasks queue name
    - WORKER_URL: Worker service URL (Cloud Run URL)

    Optional settings:
    - GCP_REGION: Region for Cloud Tasks (default: australia-southeast1)
    - CLOUD_TASKS_SERVICE_ACCOUNT: Service account for OIDC token
    """

    @property
    def dispatcher_name(self) -> str:
        return "cloud_tasks"

    @property
    def is_sync(self) -> bool:
        return False

    def is_available(self) -> bool:
        """Check if required settings are configured."""
        return all([
            getattr(settings, "GCP_PROJECT_ID", None),
            getattr(settings, "GCS_TASK_QUEUE_NAME", None),
            getattr(settings, "WORKER_URL", None),
        ])

    def dispatch(self, request: TaskDispatchRequest) -> TaskDispatchResponse:
        """Enqueue task via Google Cloud Tasks."""
        from google.cloud import tasks_v2

        # Validate configuration
        project_id = getattr(settings, "GCP_PROJECT_ID", "")
        queue_name = getattr(settings, "GCS_TASK_QUEUE_NAME", "")
        worker_url = getattr(settings, "WORKER_URL", "")

        if not project_id:
            return TaskDispatchResponse(
                task_id=None,
                is_sync=False,
                error="GCP_PROJECT_ID must be set for Cloud Tasks",
            )
        if not queue_name:
            return TaskDispatchResponse(
                task_id=None,
                is_sync=False,
                error="GCS_TASK_QUEUE_NAME must be set for Cloud Tasks",
            )
        if not worker_url:
            return TaskDispatchResponse(
                task_id=None,
                is_sync=False,
                error="WORKER_URL must be set for Cloud Tasks",
            )

        # Build the full queue path
        region = getattr(settings, "GCP_REGION", "australia-southeast1")
        queue_path = f"projects/{project_id}/locations/{region}/queues/{queue_name}"

        logger.info(
            "Cloud Tasks config: project_id=%s region=%s queue_name=%s queue_path=%s",
            project_id,
            region,
            queue_name,
            queue_path,
        )

        # Task name for logging (not for deduplication - Cloud Tasks auto-generates ID)
        if request.resume_from_step is not None:
            task_name = (
                f"validation-run-{request.validation_run_id}"
                f"-step-{request.resume_from_step}"
            )
        else:
            task_name = f"validation-run-{request.validation_run_id}"

        # Build the task URL and payload
        endpoint_url = f"{worker_url.rstrip('/')}/api/v1/execute-validation-run/"
        payload = request.to_payload()

        # Get the service account for OIDC authentication
        service_account = self._get_invoker_service_account()

        # Create the task
        client = tasks_v2.CloudTasksClient()

        task = tasks_v2.Task(
            http_request=tasks_v2.HttpRequest(
                http_method=tasks_v2.HttpMethod.POST,
                url=endpoint_url,
                headers={"Content-Type": "application/json"},
                body=json.dumps(payload).encode(),
                oidc_token=tasks_v2.OidcToken(
                    service_account_email=service_account,
                ),
            ),
        )

        create_request = tasks_v2.CreateTaskRequest(
            parent=queue_path,
            task=task,
        )

        logger.info(
            "Enqueueing Cloud Task: queue=%s task_name=%s validation_run_id=%s "
            "resume_from_step=%s",
            queue_name,
            task_name,
            request.validation_run_id,
            request.resume_from_step,
        )

        try:
            created_task = client.create_task(request=create_request)
            logger.info(
                "Cloud Task created: %s for validation_run_id=%s",
                created_task.name,
                request.validation_run_id,
            )
            return TaskDispatchResponse(
                task_id=created_task.name,
                is_sync=False,
            )

        except Exception as exc:
            # Check if it's a duplicate task (already exists)
            if "ALREADY_EXISTS" in str(exc):
                logger.info(
                    "Cloud Task already exists (dedupe): task_name=%s "
                    "validation_run_id=%s",
                    task_name,
                    request.validation_run_id,
                )
                return TaskDispatchResponse(
                    task_id=task_name,
                    is_sync=False,
                )

            logger.exception(
                "Cloud Tasks dispatcher: failed to create task for "
                "validation_run_id=%s",
                request.validation_run_id,
            )
            return TaskDispatchResponse(
                task_id=None,
                is_sync=False,
                error=str(exc),
            )

    def _get_invoker_service_account(self) -> str:
        """
        Get the service account email to use for OIDC token.

        This should be the service account that has roles/run.invoker
        permission on the worker Cloud Run service.
        """
        # Explicit setting takes precedence
        service_account = getattr(settings, "CLOUD_TASKS_SERVICE_ACCOUNT", "")
        if service_account:
            return service_account

        # Fall back to the default compute service account
        project_id = getattr(settings, "GCP_PROJECT_ID", "")
        if project_id:
            return f"{project_id}@appspot.gserviceaccount.com"

        raise ValueError(
            "CLOUD_TASKS_SERVICE_ACCOUNT or GCP_PROJECT_ID must be set",
        )
