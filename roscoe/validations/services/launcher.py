from __future__ import annotations

import contextlib
from typing import Any

from celery.exceptions import TimeoutError as CeleryTimeout
from django.conf import settings
from django.urls import reverse
from rest_framework import status
from rest_framework.response import Response

from roscoe.validations.constants import JobStatus
from roscoe.validations.models import ValidationRun
from roscoe.validations.serializers import ValidationRunSerializer
from roscoe.validations.tasks import execute_validation_run


def _is_final(status_value: str) -> bool:
    final_statuses = {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        getattr(JobStatus, "CANCELED", "canceled"),
        getattr(JobStatus, "TIMED_OUT", "timed_out"),
    }
    return status_value in final_statuses


class ValidationJobLauncher:
    """
    Orchestrates: create run, enqueue Celery, optimistic wait (poll),
    and build a uniform Response.
    """

    def launch(
        self,
        *,
        request,
        org,
        workflow,
        submission=None,
        document: dict | None = None,
        metadata: dict | None = None,
        user_id: int | None = None,
    ) -> Response:
        run = ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            submission=submission,
            project=getattr(submission, "project", None),
            status=JobStatus.PENDING,
        )

        payload = {"document": document, "metadata": metadata or {}, "user_id": user_id}
        async_result = execute_validation_run.delay(run.id, payload)

        # Poll: 5s per attempt, up to 4 attempts (total ~20s)
        per_attempt = int(getattr(settings, "VALIDATION_START_ATTEMPT_TIMEOUT", 5))
        attempts = int(getattr(settings, "VALIDATION_START_ATTEMPTS", 4))

        for _ in range(attempts):
            with contextlib.suppress(CeleryTimeout):
                async_result.get(timeout=per_attempt, propagate=False)
                # If finished within this wait, break early after refresh
            run.refresh_from_db()
            if _is_final(run.status):
                break

        location = request.build_absolute_uri(
            reverse("api:validationrun-detail", kwargs={"pk": run.id})
        )

        if _is_final(run.status):
            data = ValidationRunSerializer(run).data
            return Response(
                data, status=status.HTTP_201_CREATED, headers={"Location": location}
            )

        body = {
            "id": run.id,
            "status": run.status,
            "task_id": async_result.id,
            "detail": "Processing",
            "url": location,
        }
        headers = {
            "Location": location,
            "Retry-After": str(per_attempt),
        }
        return Response(body, status=status.HTTP_202_ACCEPTED, headers=headers)
