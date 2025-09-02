from __future__ import annotations

import logging

from attr import dataclass
from celery import shared_task
from django.utils import timezone

from roscoe.validations.constants import JobStatus
from roscoe.validations.models import ValidationRun
from roscoe.validations.services.runner import ValidationRunner

logger = logging.getLogger(__name__)


@dataclass
class ValidationRunTaskResult:
    run_id: int
    status: JobStatus
    result: dict | None = None
    error: str | None = None


@shared_task(
    bind=True,
    autoretry_for=(),
    retry_backoff=False,
)
def execute_validation_run(
    self, run_id: int, payload: dict | None = None
) -> ValidationRunTaskResult:
    run = ValidationRun.objects.select_related(
        "workflow",
        "org",
        "project",
        "submission",
    ).get(id=run_id)

    if run.status not in (JobStatus.PENDING, JobStatus.RUNNING):
        result = ValidationRunTaskResult(
            run_id=run.id,
            status=run.status,
        )
        return result

    # Mark running
    run.status = JobStatus.RUNNING
    if hasattr(run, "started_at") and not run.started_at:
        run.started_at = timezone.now()
    run.save(
        update_fields=["status", "modified"]
        + (["started_at"] if hasattr(run, "started_at") else [])
    )

    try:
        result = ValidationRunner().run(run=run, payload=payload or {})
        run.status = JobStatus.SUCCEEDED
        if hasattr(run, "finished_at"):
            run.finished_at = timezone.now()
        if hasattr(run, "summary") and isinstance(result, dict):
            run.summary = result.get("summary", "")
        if hasattr(run, "result"):
            run.result = result  # JSONField recommended
        run.save()
        result = ValidationRunTaskResult(
            run_id=run.id,
            status=run.status,
            result=result,
        )
    except Exception as exc:
        logger.exception("Validation run %s failed", run.id)
        run.status = JobStatus.FAILED
        if hasattr(run, "finished_at"):
            run.finished_at = timezone.now()
        if hasattr(run, "error"):
            run.error = str(exc)
        run.save()
        result = ValidationRunTaskResult(
            run_id=run.id,
            status=JobStatus.FAILED,
            error=str(exc),
        )

    return result
