"""Tests for Cloud Run execution status and cancellation semantics.

Cloud Run exposes deletion and cancellation as separate operations. Deletion
removes the execution resource; cancellation is the operation that requests
active compute to stop. These tests keep the runner on the cost- and
lifecycle-correct API without requiring live Google credentials.
"""

from unittest.mock import MagicMock

from django.test import SimpleTestCase
from google.cloud import run_v2

from validibot.validations.services.runners.base import ExecutionStatus
from validibot.validations.services.runners.google_cloud_run import (
    GoogleCloudRunValidatorRunner,
)


class TestGoogleCloudRunCancellation(SimpleTestCase):
    """Map logical cancellation to Cloud Run's execution-cancel API."""

    def test_cancel_requests_execution_cancellation_without_deleting_record(self):
        """Stopping active compute must preserve its provider execution record.

        Deleting an execution is a retention operation and is not a substitute
        for cancellation. Keeping the resource also leaves provider evidence
        available for reconciliation and incident review.
        """
        execution_id = "projects/p/locations/r/jobs/j/executions/e"
        client = MagicMock()
        runner = GoogleCloudRunValidatorRunner(
            project_id="test-project",
            region="us-central1",
        )
        runner._executions_client = client

        cancelled = runner.cancel(execution_id)

        assert cancelled is True
        client.cancel_execution.assert_called_once_with(name=execution_id)
        client.delete_execution.assert_not_called()

    def test_cancel_api_failure_is_reported_without_raising(self):
        """A provider outage must not undo Validibot's logical cancellation.

        Cancellation is best-effort until durable lifecycle work lands. The
        runner therefore reports failure to its caller while leaving the
        already-committed run decision intact and the provider failure visible.
        """
        execution_id = "projects/p/locations/r/jobs/j/executions/e"
        client = MagicMock()
        client.cancel_execution.side_effect = RuntimeError("API unavailable")
        runner = GoogleCloudRunValidatorRunner(
            project_id="test-project",
            region="us-central1",
        )
        runner._executions_client = client

        cancelled = runner.cancel(execution_id)

        assert cancelled is False
        client.cancel_execution.assert_called_once_with(name=execution_id)


class TestGoogleCloudRunStatus(SimpleTestCase):
    """Preserve Cloud Run terminal state independently of diagnostics."""

    def test_failed_condition_keeps_status_when_message_is_empty(self):
        """Failure state must survive an empty human-readable diagnostic.

        Reconciliation consumes this explicit status; losing it would make the
        failed execution indistinguishable from a successful job whose callback
        was lost.
        """
        condition = MagicMock()
        condition.type_ = "Completed"
        condition.state = run_v2.Condition.State.CONDITION_FAILED
        condition.execution_reason = run_v2.Condition.ExecutionReason.NON_ZERO_EXIT_CODE
        condition.message = ""
        execution = MagicMock()
        execution.conditions = [condition]
        execution.start_time = "start"
        execution.completion_time = "finish"
        client = MagicMock()
        client.get_execution.return_value = execution
        runner = GoogleCloudRunValidatorRunner(
            project_id="test-project",
            region="us-central1",
        )
        runner._executions_client = client

        info = runner.get_execution_status("execution-123")

        assert info.status == ExecutionStatus.FAILED
        assert info.error_message is None

    def test_cancelled_condition_is_not_collapsed_into_failure(self):
        """Provider cancellation remains distinguishable from execution error.

        Cloud Run represents cancellation using a failed Completed condition
        plus a specific execution reason. Reading only the generic condition
        state would erase the lifecycle event that Validibot requested.
        """
        condition = MagicMock()
        condition.type_ = "Completed"
        condition.state = run_v2.Condition.State.CONDITION_FAILED
        condition.execution_reason = run_v2.Condition.ExecutionReason.CANCELLED
        condition.message = "Execution was cancelled"
        execution = MagicMock()
        execution.conditions = [condition]
        execution.start_time = "start"
        execution.completion_time = "finish"
        client = MagicMock()
        client.get_execution.return_value = execution
        runner = GoogleCloudRunValidatorRunner(
            project_id="test-project",
            region="us-central1",
        )
        runner._executions_client = client

        info = runner.get_execution_status("execution-123")

        assert info.status == ExecutionStatus.CANCELLED
