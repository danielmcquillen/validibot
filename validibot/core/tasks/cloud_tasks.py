"""
Cloud Tasks client for enqueuing validation run execution tasks.

This module handles enqueueing tasks to Google Cloud Tasks for validation run
execution. The worker service receives these tasks and processes them via
the execute-validation-run endpoint.

Architecture:
    Web Instance -> Cloud Tasks Queue -> Worker Instance
                                        POST /api/v1/execute-validation-run/

For local development (DEBUG=True without GCS_TASK_QUEUE_NAME), the module
calls the worker directly via HTTP, bypassing Cloud Tasks infrastructure.

See ADR-001 for detailed architecture documentation.
"""

from __future__ import annotations

import json
import logging
import sys

from django.conf import settings

logger = logging.getLogger(__name__)


def enqueue_validation_run(
    validation_run_id: int,
    user_id: int,
    resume_from_step: int | None = None,
) -> str | None:
    """
    Enqueue a validation run execution task to Cloud Tasks.

    This function creates a Cloud Task that will POST to the worker's
    execute-validation-run endpoint. For initial execution, resume_from_step
    is None. For resume after an async step callback, it contains the step
    order to start from.

    Args:
        validation_run_id: ID of the ValidationRun to execute.
        user_id: ID of the user who initiated the run.
        resume_from_step: Step order to resume from (None for initial execution).

    Returns:
        The task name if successfully enqueued, None for local dev direct calls.

    Raises:
        google.api_core.exceptions.GoogleAPICallError: If task creation fails.
    """
    payload = {
        "validation_run_id": validation_run_id,
        "user_id": user_id,
        "resume_from_step": resume_from_step,
    }

    queue_name = getattr(settings, "GCS_TASK_QUEUE_NAME", None)

    # Test environment: execute synchronously (no HTTP or Cloud Tasks)
    # Check for TESTING flag or pytest presence to detect test runs
    is_testing = getattr(settings, "TESTING", False) or "pytest" in sys.modules
    if is_testing and not queue_name:
        return _enqueue_test(payload)

    # Local development: call worker directly via HTTP
    if settings.DEBUG and not queue_name:
        return _enqueue_local_dev(payload)

    # Production: enqueue via Cloud Tasks
    return _enqueue_cloud_tasks(
        validation_run_id=validation_run_id,
        resume_from_step=resume_from_step,
        payload=payload,
    )


def _enqueue_test(payload: dict) -> None:
    """
    Test environment fallback: execute synchronously inline.

    This bypasses Cloud Tasks and HTTP entirely, calling execute() directly.
    This is the behavior tests expect - synchronous execution within the
    same process, without needing to mock HTTP calls or Cloud Tasks.
    """
    from validibot.validations.services.validation_run import ValidationRunService

    logger.info(
        "Test mode: executing synchronously for validation_run_id=%s",
        payload["validation_run_id"],
    )

    service = ValidationRunService()
    service.execute(
        validation_run_id=payload["validation_run_id"],
        user_id=payload["user_id"],
        metadata=None,
        resume_from_step=payload.get("resume_from_step"),
    )


def _enqueue_local_dev(payload: dict) -> None:
    """
    Local development fallback: call worker directly via HTTP.

    This bypasses Cloud Tasks and calls the worker service directly.
    Requires the worker container to be running on port 8001.
    """
    import httpx

    worker_url = "http://worker:8001/api/v1/execute-validation-run/"

    logger.info(
        "Local dev: calling worker directly for validation_run_id=%s",
        payload["validation_run_id"],
    )

    try:
        response = httpx.post(
            worker_url,
            json=payload,
            timeout=300,
        )
        response.raise_for_status()
        logger.info(
            "Local dev: worker returned %s for validation_run_id=%s",
            response.status_code,
            payload["validation_run_id"],
        )
    except httpx.HTTPError as exc:
        logger.exception(
            "Local dev: failed to call worker for validation_run_id=%s",
            payload["validation_run_id"],
        )
        raise RuntimeError(
            f"Failed to call worker directly: {exc}",
        ) from exc


def _enqueue_cloud_tasks(
    validation_run_id: int,
    resume_from_step: int | None,
    payload: dict,
) -> str:
    """
    Enqueue task to Google Cloud Tasks.

    Creates a task that will POST to the worker's execute-validation-run
    endpoint with OIDC authentication.
    """
    from google.cloud import tasks_v2

    # Get configuration from settings
    project_id = getattr(settings, "GCP_PROJECT_ID", "")
    queue_name = getattr(settings, "GCS_TASK_QUEUE_NAME", "")
    worker_url = getattr(settings, "WORKER_URL", "")

    if not project_id:
        raise ValueError("GCP_PROJECT_ID must be set for Cloud Tasks")
    if not queue_name:
        raise ValueError("GCS_TASK_QUEUE_NAME must be set for Cloud Tasks")
    if not worker_url:
        raise ValueError("WORKER_URL must be set for Cloud Tasks")

    # Build the full queue path
    region = getattr(settings, "GCP_REGION", "australia-southeast1")
    queue_path = f"projects/{project_id}/locations/{region}/queues/{queue_name}"

    # Task name for deduplication
    # Initial execution: validation-run-{id}
    # Resume execution: validation-run-{id}-step-{resume_from_step}
    if resume_from_step is not None:
        task_name = f"validation-run-{validation_run_id}-step-{resume_from_step}"
    else:
        task_name = f"validation-run-{validation_run_id}"

    full_task_name = f"{queue_path}/tasks/{task_name}"

    # Build the task URL
    endpoint_url = f"{worker_url.rstrip('/')}/api/v1/execute-validation-run/"

    # Get the service account for OIDC authentication
    # The worker service validates the OIDC token via IAM
    service_account = _get_invoker_service_account()

    # Create the task
    client = tasks_v2.CloudTasksClient()

    task = tasks_v2.Task(
        name=full_task_name,
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

    request = tasks_v2.CreateTaskRequest(
        parent=queue_path,
        task=task,
    )

    logger.info(
        "Enqueueing Cloud Task: queue=%s task_name=%s validation_run_id=%s "
        "resume_from_step=%s",
        queue_name,
        task_name,
        validation_run_id,
        resume_from_step,
    )

    try:
        created_task = client.create_task(request=request)
        logger.info(
            "Cloud Task created: %s for validation_run_id=%s",
            created_task.name,
            validation_run_id,
        )
        return created_task.name
    except Exception as exc:
        # Check if it's a duplicate task (already exists)
        # Cloud Tasks returns ALREADY_EXISTS if task name is taken
        if "ALREADY_EXISTS" in str(exc):
            logger.info(
                "Cloud Task already exists (dedupe): %s for validation_run_id=%s",
                full_task_name,
                validation_run_id,
            )
            return full_task_name
        raise


def _get_invoker_service_account() -> str:
    """
    Get the service account email to use for OIDC token.

    This should be the service account that has roles/run.invoker
    permission on the worker Cloud Run service.

    Returns:
        Service account email for OIDC authentication.
    """
    # In production, this is typically the Cloud Tasks service account
    # or a dedicated invoker service account
    service_account = getattr(settings, "CLOUD_TASKS_SERVICE_ACCOUNT", "")
    if service_account:
        return service_account

    # Fall back to the default compute service account
    project_id = getattr(settings, "GCP_PROJECT_ID", "")
    if project_id:
        # Default compute service account format
        return f"{project_id}@appspot.gserviceaccount.com"

    raise ValueError(
        "CLOUD_TASKS_SERVICE_ACCOUNT or GCP_PROJECT_ID must be set",
    )
