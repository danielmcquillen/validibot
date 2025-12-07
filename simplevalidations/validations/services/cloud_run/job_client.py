"""
Cloud Run Job client using Cloud Tasks for async execution.

This module triggers Cloud Run Jobs by creating Cloud Tasks that invoke
the jobs. This provides better retry logic and decoupling than calling
the Cloud Run Jobs API directly.

Design: Simple functions that create tasks. No complex state management.

Why Cloud Tasks:
- Built-in retry logic with exponential backoff
- Rate limiting and quota management
- Better observability via Cloud Tasks console
- Decouples Django from job execution timing
"""

import json

from google.cloud import tasks_v2
from google.protobuf import duration_pb2


def trigger_validator_job(
    *,
    project_id: str,
    region: str,
    queue_name: str,
    job_name: str,
    input_uri: str,
    service_account_email: str | None = None,
    timeout_seconds: int = 1800,  # Max allowed by Cloud Tasks is 30 minutes
) -> str:
    """
    Trigger a Cloud Run Job by creating a Cloud Task.

    This function creates a task that will execute the specified Cloud Run Job
    with the given input URI as an environment variable.

    The Cloud Task will invoke the Cloud Run Jobs Execution API to create a new
    job execution. The job container will receive INPUT_URI as an environment
    variable and load its input envelope from that GCS location.

    Args:
        project_id: GCP project ID
        region: GCP region (e.g., 'us-central1')
        queue_name: Cloud Tasks queue name (e.g., 'validator-jobs')
        job_name: Cloud Run Job name (e.g., 'validibot-validator-energyplus')
        input_uri: GCS URI to input.json (e.g., 'gs://bucket/runs/abc/input.json')
        service_account_email: Service account to use for OIDC authentication.
            Defaults to 'validibot-cloudrun-prod@{project_id}.iam.gserviceaccount.com'
        timeout_seconds: Task dispatch deadline in seconds (default: 1800 = 30 min).
            Cloud Tasks allows max 30 minutes. The Cloud Run Job itself can run longer.

    Returns:
        Task name (can be used for tracking/monitoring)

    Raises:
        google.cloud.exceptions.GoogleCloudError: If task creation fails

    Example:
        >>> task_name = trigger_validator_job(
        ...     project_id="my-project",
        ...     region="us-central1",
        ...     queue_name="validator-jobs",
        ...     job_name="validibot-validator-energyplus",
        ...     input_uri="gs://my-bucket/runs/abc-123/input.json",
        ... )
        >>> print(f"Created task: {task_name}")
    """
    # Initialize Cloud Tasks client
    client = tasks_v2.CloudTasksClient()

    # Construct the queue path
    parent = client.queue_path(project_id, region, queue_name)

    # Create the HTTP request task
    # The Cloud Tasks queue should be configured to call the Cloud Run Jobs API
    # using the queue's service account with roles/run.invoker permission
    task = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"https://run.googleapis.com/v2/projects/{project_id}/locations/{region}/jobs/{job_name}:run",
            "headers": {
                "Content-Type": "application/json",
            },
            "body": json.dumps({
                "overrides": {
                    "container_overrides": [
                        {
                            "env": [
                                {
                                    "name": "INPUT_URI",
                                    "value": input_uri,
                                }
                            ]
                        }
                    ]
                }
            }).encode(),
            "oidc_token": {
                # Use the provided service account to authenticate
                # The service account must have roles/run.invoker on the job
                "service_account_email": (
                    service_account_email
                    or f"validibot-cloudrun-prod@{project_id}.iam.gserviceaccount.com"
                ),
            },
        },
        "dispatch_deadline": duration_pb2.Duration(seconds=timeout_seconds),
    }

    # Create the task
    response = client.create_task(
        request={
            "parent": parent,
            "task": task,
        }
    )

    return response.name


def get_task_status(task_name: str) -> dict:
    """
    Get the status of a Cloud Task.

    Args:
        task_name: Full task name (returned from trigger_validator_job)

    Returns:
        Dictionary with task status information

    Raises:
        google.cloud.exceptions.GoogleCloudError: If status check fails

    Example:
        >>> status = get_task_status(task_name)
        >>> print(status["state"])  # PENDING, RUNNING, SUCCEEDED, etc.
    """
    client = tasks_v2.CloudTasksClient()
    task = client.get_task(name=task_name)

    return {
        "name": task.name,
        "dispatch_count": task.dispatch_count,
        "response_count": task.response_count,
        "first_attempt": task.first_attempt,
        "last_attempt": task.last_attempt,
        "schedule_time": task.schedule_time,
    }
