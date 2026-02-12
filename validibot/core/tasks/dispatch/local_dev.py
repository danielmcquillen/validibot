"""
Local development task dispatcher.

Calls the worker service directly via HTTP, bypassing task queues.
Requires the worker container to be running on port 8001.
"""

from __future__ import annotations

import logging

from django.conf import settings

from validibot.core.tasks.dispatch.base import TaskDispatcher
from validibot.core.tasks.dispatch.base import TaskDispatchRequest
from validibot.core.tasks.dispatch.base import TaskDispatchResponse

logger = logging.getLogger(__name__)


class LocalDevDispatcher(TaskDispatcher):
    """
    Local development dispatcher - direct HTTP call to worker.

    Bypasses task queues and calls the worker service directly.
    Useful for local development where you want to test the full flow
    without setting up Redis/Celery.

    If WORKER_API_KEY is configured, the key is included in the
    Authorization header for authentication.

    Requires the worker container to be running on port 8001.
    """

    WORKER_URL = "http://worker:8001/api/v1/execute-validation-run/"
    TIMEOUT_SECONDS = 300

    @property
    def dispatcher_name(self) -> str:
        return "local_dev"

    @property
    def is_sync(self) -> bool:
        # HTTP call blocks until worker completes
        return True

    def is_available(self) -> bool:
        # We could ping the worker, but for simplicity assume available
        return True

    def _get_headers(self) -> dict[str, str]:
        """Build request headers, including worker API key if configured."""
        headers: dict[str, str] = {}
        worker_key = getattr(settings, "WORKER_API_KEY", "")
        if worker_key:
            headers["Authorization"] = f"Worker-Key {worker_key}"
        return headers

    def dispatch(self, request: TaskDispatchRequest) -> TaskDispatchResponse:
        """Call worker directly via HTTP."""
        import httpx

        payload = request.to_payload()

        logger.info(
            "Local dev dispatcher: calling worker for validation_run_id=%s",
            request.validation_run_id,
        )

        try:
            response = httpx.post(
                self.WORKER_URL,
                json=payload,
                headers=self._get_headers(),
                timeout=self.TIMEOUT_SECONDS,
            )
            response.raise_for_status()

            logger.info(
                "Local dev dispatcher: worker returned %s for validation_run_id=%s",
                response.status_code,
                request.validation_run_id,
            )
            return TaskDispatchResponse(task_id=None, is_sync=True)

        except httpx.HTTPError as exc:
            logger.exception(
                "Local dev dispatcher: failed to call worker for validation_run_id=%s",
                request.validation_run_id,
            )
            return TaskDispatchResponse(
                task_id=None,
                is_sync=True,
                error=f"Failed to call worker: {exc}",
            )
