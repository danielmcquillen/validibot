"""Integration tests for reader-first runtime-profile routing at worker edges.

Stage 1 does not create attempt-mode runs, but a rolling deployment or bad
downgrade could deliver one to a legacy worker.  These tests prove execution,
callbacks, watchdog reconciliation, and cancellation either reject that mode
before reading legacy metadata or use the additive attempt identity where the
operation is already safe.
"""

import uuid
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from rest_framework import status

from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunErrorCategory
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationRuntimeProfile
from validibot.validations.management.commands.cleanup_stuck_runs import Command
from validibot.validations.models import CallbackReceipt
from validibot.validations.services.step_orchestrator import StepOrchestrator
from validibot.validations.services.validation_callback import ValidationCallbackService
from validibot.validations.services.validation_run import ValidationRunService
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory

RUNNER_FACTORY_PATH = "validibot.validations.services.runners.get_validator_runner"


@pytest.mark.django_db
class TestRuntimeProfileRouting:
    """Fence attempt-mode work at every legacy execution entry point."""

    @patch("validibot.validations.signals.validation_run_finalized.send_robust")
    def test_orchestrator_rejects_attempt_profile_before_starting_steps(
        self,
        mock_finalized,
    ):
        """A legacy worker must not dispatch an attempt run using legacy metadata."""
        run = ValidationRunFactory(
            runtime_profile=ValidationRuntimeProfile.ATTEMPT_LIFECYCLE_V1,
            status=ValidationRunStatus.PENDING,
        )

        result = StepOrchestrator().execute_workflow_steps(run.id, run.user_id)

        run.refresh_from_db()
        assert result.status == ValidationRunStatus.FAILED
        assert run.status == ValidationRunStatus.FAILED
        assert run.error_category == ValidationRunErrorCategory.SYSTEM_ERROR
        assert run.step_runs.count() == 0
        mock_finalized.assert_called_once()

    @patch("validibot.validations.signals.validation_run_finalized.send_robust")
    def test_direct_step_dispatch_cannot_bypass_profile_guard(self, mock_finalized):
        """The compatibility facade must not offer an unguarded legacy side door."""
        run = ValidationRunFactory(
            runtime_profile=ValidationRuntimeProfile.ATTEMPT_LIFECYCLE_V1,
            status=ValidationRunStatus.RUNNING,
        )

        result = StepOrchestrator().execute_workflow_step(MagicMock(), run)

        run.refresh_from_db()
        assert result.passed is False
        assert result.stats == {"runtime_profile_rejected": True}
        assert run.status == ValidationRunStatus.FAILED
        mock_finalized.assert_called_once()

    @patch("validibot.validations.signals.validation_run_finalized.send_robust")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_callback_rejects_attempt_profile_before_storage_read(
        self,
        mock_download,
        mock_finalized,
    ):
        """An old callback handler must not parse strict output as a legacy envelope."""
        run = ValidationRunFactory(
            runtime_profile=ValidationRuntimeProfile.ATTEMPT_LIFECYCLE_V1,
            status=ValidationRunStatus.RUNNING,
        )
        callback_id = str(uuid.uuid4())

        response = ValidationCallbackService().process(
            payload={
                "run_id": str(run.id),
                "callback_id": callback_id,
                "status": "success",
                "result_uri": "gs://bucket/output.json",
            }
        )

        run.refresh_from_db()
        assert response.status_code == status.HTTP_200_OK
        assert response.data["runtime_profile_rejected"] is True
        assert run.status == ValidationRunStatus.FAILED
        assert not CallbackReceipt.objects.filter(callback_id=callback_id).exists()
        mock_download.assert_not_called()
        mock_finalized.assert_called_once()

    @patch("validibot.validations.signals.validation_run_finalized.send_robust")
    def test_watchdog_dry_run_reports_without_mutating_then_real_run_fences(
        self,
        mock_finalized,
    ):
        """Dry-run remains truthful while the real legacy watchdog fails closed."""
        run = ValidationRunFactory(
            runtime_profile=ValidationRuntimeProfile.ATTEMPT_LIFECYCLE_V1,
            status=ValidationRunStatus.RUNNING,
        )
        command = Command()

        dry_result = command._try_reconcile_gcp_run(run, dry_run=True)
        run.refresh_from_db()
        assert dry_result == "profile_rejected"
        assert run.status == ValidationRunStatus.RUNNING
        mock_finalized.assert_not_called()

        real_result = command._try_reconcile_gcp_run(run)
        run.refresh_from_db()
        assert real_result == "profile_rejected"
        assert run.status == ValidationRunStatus.FAILED
        mock_finalized.assert_called_once()

    @patch(RUNNER_FACTORY_PATH)
    def test_cancellation_reads_provider_identity_from_attempt(self, mock_get_runner):
        """Logical cancellation can safely stop attempt work before Stage 3 writers."""
        run = ValidationRunFactory(
            runtime_profile=ValidationRuntimeProfile.ATTEMPT_LIFECYCLE_V1,
            status=ValidationRunStatus.RUNNING,
        )
        step_run = ValidationStepRunFactory(
            validation_run=run,
            status=StepStatus.RUNNING,
            output={"execution_name": "stale-legacy-execution"},
        )
        attempt = ExecutionAttemptFactory(
            step_run=step_run,
            state=ExecutionAttemptState.RUNNING,
            provider_execution_id="attempt-execution",
        )
        runner = MagicMock()
        runner.cancel.return_value = True
        mock_get_runner.return_value = runner

        updated_run, canceled = ValidationRunService().cancel_run(run=run)

        assert canceled is True
        updated_run.refresh_from_db()
        assert updated_run.status == ValidationRunStatus.CANCELED
        runner.cancel.assert_called_once_with(attempt.provider_execution_id)
