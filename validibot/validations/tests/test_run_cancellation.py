"""Regression tests for logical and provider validation-run cancellation.

The database decision is authoritative and must commit even when an external
runner is unavailable. When a concrete execution identity is known, Validibot
should then ask the configured runner to stop that work so canceled runs do not
continue consuming resources.
"""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.services.validation_run import ValidationRunService
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory

RUNNER_FACTORY_PATH = "validibot.validations.services.runners.get_validator_runner"


@pytest.mark.django_db
class TestValidationRunCancellation:
    """Keep user-visible cancellation authoritative over provider effects."""

    @patch(RUNNER_FACTORY_PATH)
    def test_running_execution_is_canceled_after_run_is_fenced(
        self,
        mock_get_runner,
    ):
        """A known execution must be stopped after the run becomes CANCELED.

        The runner call observes the database's terminal decision, ensuring a
        provider failure cannot roll back user intent or let a late callback
        legitimately reopen the run.
        """
        execution_id = "projects/p/locations/r/jobs/j/executions/e"
        run = ValidationRunFactory(status=ValidationRunStatus.RUNNING)
        ValidationStepRunFactory(
            validation_run=run,
            status=StepStatus.RUNNING,
            output={"execution_name": execution_id},
        )
        runner = MagicMock()
        runner.cancel.return_value = True
        mock_get_runner.return_value = runner

        updated_run, canceled = ValidationRunService().cancel_run(run=run)

        assert canceled is True
        updated_run.refresh_from_db()
        assert updated_run.status == ValidationRunStatus.CANCELED
        runner.cancel.assert_called_once_with(execution_id)

    @patch(RUNNER_FACTORY_PATH)
    def test_provider_failure_does_not_undo_logical_cancellation(
        self,
        mock_get_runner,
    ):
        """An unavailable provider must leave the run terminally canceled.

        PostgreSQL and the provider cannot share a transaction. The safe order
        is to commit the logical fence first and treat external cancellation
        failure as observable cleanup work rather than reopening the run.
        """
        run = ValidationRunFactory(status=ValidationRunStatus.RUNNING)
        ValidationStepRunFactory(
            validation_run=run,
            status=StepStatus.RUNNING,
            output={"execution_name": "execution-123"},
        )
        runner = MagicMock()
        runner.cancel.return_value = False
        mock_get_runner.return_value = runner

        _, canceled = ValidationRunService().cancel_run(run=run)

        assert canceled is True
        run.refresh_from_db()
        assert run.status == ValidationRunStatus.CANCELED
