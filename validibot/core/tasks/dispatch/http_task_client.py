"""Low-level Google Cloud Tasks HTTP-target creation.

Application orchestration and validator-provider delivery use separate queues,
identities, deadlines, and idempotency policies.  This module shares only the
small transport primitive so neither higher-level dispatcher can accidentally
inherit the other's semantics.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from google.api_core.exceptions import AlreadyExists
from google.protobuf import duration_pb2


@dataclass(frozen=True, slots=True)
class HttpTaskCreationResult:
    """Provider task identity and whether this call created it."""

    task_name: str
    created: bool


def create_http_task(
    *,
    project_id: str,
    region: str,
    queue_name: str,
    endpoint_url: str,
    payload: dict,
    oidc_service_account: str,
    oidc_audience: str,
    dispatch_deadline_seconds: int,
    task_id: str | None = None,
) -> HttpTaskCreationResult:
    """Create one authenticated HTTP task, converging on deterministic names."""
    from google.cloud import tasks_v2

    queue_path = f"projects/{project_id}/locations/{region}/queues/{queue_name}"
    task_name = f"{queue_path}/tasks/{task_id}" if task_id else ""
    task = tasks_v2.Task(
        name=task_name,
        http_request=tasks_v2.HttpRequest(
            http_method=tasks_v2.HttpMethod.POST,
            url=endpoint_url,
            headers={"Content-Type": "application/json"},
            body=json.dumps(payload, separators=(",", ":")).encode(),
            oidc_token=tasks_v2.OidcToken(
                service_account_email=oidc_service_account,
                audience=oidc_audience,
            ),
        ),
        dispatch_deadline=duration_pb2.Duration(
            seconds=dispatch_deadline_seconds,
        ),
    )
    client = tasks_v2.CloudTasksClient()
    try:
        created = client.create_task(
            request=tasks_v2.CreateTaskRequest(parent=queue_path, task=task)
        )
    except AlreadyExists:
        if not task_name:
            raise
        return HttpTaskCreationResult(task_name=task_name, created=False)
    return HttpTaskCreationResult(task_name=created.name, created=True)
