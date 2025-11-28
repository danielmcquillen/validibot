from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from celery import shared_task
from django.conf import settings

from simplevalidations.validations.services.fmi import run_fmu_probe
from simplevalidations.validations.services.validation_run import ValidationRunService

if TYPE_CHECKING:
    from simplevalidations.validations.models import FMUModel
    from simplevalidations.validations.services.models import ValidationRunTaskResult

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

    # If we're running locally or testing we may want to simulate long-running tasks.
    if getattr(settings, "SIMULATE_LONG_TASKS", False):
        delay_seconds = int(getattr(settings, "LONG_TASK_DELAY_SECONDS", 0))
        if delay_seconds > 0:
            logger.debug(
                "Simulating %s second delay for validation_run_id=%s",
                delay_seconds,
                validation_run_id,
            )
            time.sleep(delay_seconds)

    service = ValidationRunService()
    result: ValidationRunTaskResult = service.execute(
        validation_run_id=validation_run_id,
        user_id=user_id,
        metadata=metadata,
    )

    # We return a result, even though Celery doesn't do anything with it.
    # The way this result gets to the API caller is via the DB record.
    return result.to_payload()


@shared_task(
    bind=True,
    autoretry_for=(),
    retry_backoff=False,
)
def run_fmu_probe_task(self, fmu_model_id: int) -> dict:
    """
    Execute a probe run for a stored FMU.

    Probes parse modelDescription.xml and refresh FMU variables/catalog entries
    without executing arbitrary native code in-process.
    """

    from simplevalidations.validations.models import FMUModel

    fmu: FMUModel = FMUModel.objects.get(pk=fmu_model_id)
    result = run_fmu_probe(fmu)
    return result.model_dump(mode="json")
