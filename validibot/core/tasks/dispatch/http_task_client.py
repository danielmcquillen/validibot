"""Low-level Google Cloud Tasks HTTP-target creation.

Application orchestration and validator-provider delivery use separate queues,
identities, deadlines, and idempotency policies.  This module shares only the
small transport primitive so neither higher-level dispatcher can accidentally
inherit the other's semantics.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from functools import lru_cache

from google.api_core.exceptions import AlreadyExists
from google.protobuf import duration_pb2

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HttpTaskCreationResult:
    """Provider task identity and whether this call created it."""

    task_name: str
    created: bool


@lru_cache(maxsize=1)
def get_cloud_tasks_client():
    """Return one lazy Cloud Tasks client per application process.

    Google client objects retain their transport channel and credential cache.
    This getter is first called in a request/worker process, after the process
    model has forked, and the resulting gRPC client is safe to share between
    that process's threads.
    """
    from google.cloud import tasks_v2

    started = time.perf_counter()
    client = tasks_v2.CloudTasksClient()
    logger.info(
        "Initialized shared Cloud Tasks client",
        extra={
            "gcp_client": "CloudTasksClient",
            "client_initialization_ms": (time.perf_counter() - started) * 1000,
        },
    )
    return client


def clear_cloud_tasks_client_cache() -> None:
    """Clear the process client cache for tests or a settings reset."""
    get_cloud_tasks_client.cache_clear()


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
    client = get_cloud_tasks_client()
    rpc_started = time.perf_counter()
    try:
        created = client.create_task(
            request=tasks_v2.CreateTaskRequest(parent=queue_path, task=task)
        )
    except AlreadyExists:
        if not task_name:
            raise
        logger.info(
            "Cloud Tasks create converged on an existing task",
            extra={
                "gcp_client": "CloudTasksClient",
                "gcp_operation": "create_task",
                "gcp_queue_path": queue_path,
                "gcp_rpc_duration_ms": (time.perf_counter() - rpc_started) * 1000,
                "task_created": False,
            },
        )
        return HttpTaskCreationResult(task_name=task_name, created=False)
    except Exception:
        logger.warning(
            "Cloud Tasks create failed",
            extra={
                "gcp_client": "CloudTasksClient",
                "gcp_operation": "create_task",
                "gcp_queue_path": queue_path,
                "gcp_rpc_duration_ms": (time.perf_counter() - rpc_started) * 1000,
                "task_created": None,
            },
            exc_info=True,
        )
        raise
    logger.info(
        "Cloud Tasks create completed",
        extra={
            "gcp_client": "CloudTasksClient",
            "gcp_operation": "create_task",
            "gcp_queue_path": queue_path,
            "gcp_rpc_duration_ms": (time.perf_counter() - rpc_started) * 1000,
            "task_created": True,
        },
    )
    return HttpTaskCreationResult(task_name=created.name, created=True)
