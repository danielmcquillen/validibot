"""Regression tests for callbacks racing terminal run decisions.

Callbacks are asynchronous and can arrive after user cancellation or watchdog
timeout. Those deliveries may be acknowledged, but they must not download or
apply outputs, enqueue more work, or overwrite the terminal decision with a
stale in-memory RUNNING run.
"""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework import status
from validibot_shared.validations.envelopes import ValidationStatus

from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import ValidationRun
from validibot.validations.services.validation_callback import ValidationCallbackService
from validibot.validations.tests.factories import ValidationRunFactory


@pytest.mark.django_db
class TestCallbackTerminalFencing:
    """Prove callback delivery cannot reopen a terminal validation run."""

    @pytest.mark.parametrize(
        "terminal_status",
        [ValidationRunStatus.CANCELED, ValidationRunStatus.TIMED_OUT],
    )
    def test_callback_for_terminal_run_is_acknowledged_without_processing(
        self,
        terminal_status,
    ):
        """Late callbacks must stop before storage reads or step mutation.

        Returning 200 prevents pointless provider retries, while bypassing the
        processing pipeline preserves cancellation/timeout as the authoritative
        outcome and avoids trusting stale output after the deadline.
        """
        run = ValidationRunFactory(status=terminal_status)
        service = ValidationCallbackService()

        with patch.object(service, "_process_callback") as process:
            response = service.process(
                payload={
                    "run_id": str(run.id),
                    "callback_id": f"late-{run.id}",
                    "status": "success",
                    "result_uri": "gs://bucket/output.json",
                }
            )

        assert response.status_code == status.HTTP_200_OK
        assert response.data["late_callback_ignored"] is True
        process.assert_not_called()

    def test_finalization_compare_and_set_preserves_concurrent_cancel(self):
        """A cancel winning after callback admission must remain terminal.

        The callback holds a stale RUNNING model instance in this race. Its
        final write must be conditional on the database still being active;
        an ordinary model save would overwrite the concurrent CANCELED state.
        """
        run = ValidationRunFactory(
            status=ValidationRunStatus.RUNNING,
            started_at=timezone.now(),
        )
        stale_run = ValidationRun.objects.get(pk=run.pk)
        ValidationRun.objects.filter(pk=run.pk).update(
            status=ValidationRunStatus.CANCELED,
        )
        result = MagicMock()
        result.step_status = StepStatus.PASSED
        result.step_error = ""
        result.output_envelope.status = ValidationStatus.SUCCESS
        result.output_envelope.timing.finished_at = timezone.now()

        ValidationCallbackService()._finalize_run(stale_run, result)

        run.refresh_from_db()
        assert run.status == ValidationRunStatus.CANCELED
