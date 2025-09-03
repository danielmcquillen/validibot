from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from celery import shared_task

from roscoe.validations.services.validation_run import ValidationRunService

if TYPE_CHECKING:
    from roscoe.validations.services.runner import ValidationRunTaskPayload

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    autoretry_for=(),
    retry_backoff=False,
)
def execute_validation_run(
    self,
    run_id: int,
    payload: ValidationRunTaskPayload | None = None,
) -> dict:
    service = ValidationRunService()
    return service.execute(run_id, payload or {})
