from __future__ import annotations

import logging

from celery import shared_task

from roscoe.validations.services.models import ValidationRunTaskResult
from roscoe.validations.services.validation_run import ValidationRunService

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    autoretry_for=(),
    retry_backoff=False,
)
def execute_validation_run(
    self,
    validation_run_id: int,
    user_id: int | None = None,
    metadata: dict | None = None,
) -> ValidationRunTaskResult:
    """
    Celery task to execute a validation run.
    This is a thin wrapper around the ValidationRunService.
    """
    service = ValidationRunService()
    result: ValidationRunTaskResult = service.execute(
        validation_run_id=validation_run_id,
        user_id=user_id,
        metadata=metadata,
    )

    # We return a result, even though Celery doesn't do anything with it.
    # The way this result gets to the API caller is via the DB record.
    return result
