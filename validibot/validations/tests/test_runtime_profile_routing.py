"""Integration tests for attempt-lifecycle routing at worker edges.

The writer release accepts the lifecycle profile while strict-I/O profiles
remain fenced. These tests prove attempt callbacks require durable identity,
watchdog recovery reads attempt state, and cancellation fences the attempt
before asking the provider to stop.
"""

import uuid
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from rest_framework import status

from validibot.validations.constants import ExecutionAttemptState
from validibot.validations.constants import StepStatus
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
    """Route attempt-mode work without falling back to legacy metadata."""

    @patch("validibot.validations.signals.validation_run_finalized.send_robust")
    def test_orchestrator_accepts_attempt_profile(
        self,
        mock_finalized,
    ):
        """The writer release must execute lifecycle-profile runs normally."""
        run = ValidationRunFactory(
            runtime_profile=ValidationRuntimeProfile.ATTEMPT_LIFECYCLE_V1,
            status=ValidationRunStatus.PENDING,
        )

        result = StepOrchestrator().execute_workflow_steps(run.id, run.user_id)

        run.refresh_from_db()
        assert result.status == ValidationRunStatus.SUCCEEDED
        assert run.status == ValidationRunStatus.SUCCEEDED
        assert run.step_runs.count() == 0
        mock_finalized.assert_called_once()

    @patch("validibot.validations.signals.validation_run_finalized.send_robust")
    def test_direct_step_dispatch_accepts_lifecycle_profile(self, mock_finalized):
        """The compatibility facade must route the completed profile rung."""
        run = ValidationRunFactory(
            runtime_profile=ValidationRuntimeProfile.ATTEMPT_LIFECYCLE_V1,
            status=ValidationRunStatus.RUNNING,
        )

        result = StepOrchestrator().execute_workflow_step(MagicMock(), run)

        run.refresh_from_db()
        assert result.passed is False
        assert result.stats == {}
        assert run.status == ValidationRunStatus.RUNNING
        mock_finalized.assert_not_called()

    @patch("validibot.validations.signals.validation_run_finalized.send_robust")
    @patch("validibot.validations.services.validation_callback.download_envelope")
    def test_callback_rejects_unbound_attempt_id_before_storage_read(
        self,
        mock_download,
        mock_finalized,
    ):
        """Attempt-mode output must identify the concrete launch that produced it."""
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
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert run.status == ValidationRunStatus.RUNNING
        assert not CallbackReceipt.objects.filter(callback_id=callback_id).exists()
        mock_download.assert_not_called()
        mock_finalized.assert_not_called()

    @patch("validibot.validations.signals.validation_run_finalized.send_robust")
    def test_watchdog_accepts_attempt_profile_without_provider_identity(
        self,
        mock_finalized,
    ):
        """A not-yet-addressable attempt remains for timeout/operator handling."""
        run = ValidationRunFactory(
            runtime_profile=ValidationRuntimeProfile.ATTEMPT_LIFECYCLE_V1,
            status=ValidationRunStatus.RUNNING,
        )
        command = Command()

        dry_result = command._try_reconcile_gcp_run(run, dry_run=True)
        run.refresh_from_db()
        assert dry_result == "not_applicable"
        assert run.status == ValidationRunStatus.RUNNING
        mock_finalized.assert_not_called()

        real_result = command._try_reconcile_gcp_run(run)
        run.refresh_from_db()
        assert real_result == "not_applicable"
        assert run.status == ValidationRunStatus.RUNNING
        mock_finalized.assert_not_called()

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
        attempt.refresh_from_db()
        assert updated_run.status == ValidationRunStatus.CANCELED
        assert attempt.state == ExecutionAttemptState.CANCELED
        runner.cancel.assert_called_once_with(attempt.provider_execution_id)
