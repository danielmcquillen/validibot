"""Tests for StepOrchestrator internal methods.

Covers step lifecycle (_start_step_run) idempotency and
the _record_step_result normalization precondition.
"""

from __future__ import annotations

import pytest
from django.utils import timezone

from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.models import ValidationFinding
from validibot.validations.models import ValidationStepRun
from validibot.validations.services.step_orchestrator import StepOrchestrator
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

# ---------- _start_step_run ----------


@pytest.mark.django_db
class TestStartStepRun:
    """Test _start_step_run idempotency and retry behavior."""

    def setup_method(self):
        self.orchestrator = StepOrchestrator()
        self.run = ValidationRunFactory()
        self.wf_step = WorkflowStepFactory(workflow=self.run.workflow)

    def test_new_step_creates_running_step_run(self):
        """First call creates a new step run with RUNNING status."""
        step_run, should_execute = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )

        assert should_execute is True
        assert step_run.status == StepStatus.RUNNING
        assert step_run.started_at is not None
        assert step_run.step_order == (self.wf_step.order or 0)

    @pytest.mark.parametrize(
        "terminal_status",
        [
            StepStatus.PASSED,
            StepStatus.FAILED,
            StepStatus.SKIPPED,
        ],
    )
    def test_terminal_step_skips_execution(self, terminal_status):
        """A step that already finished returns should_execute=False."""
        existing = ValidationStepRunFactory(
            validation_run=self.run,
            workflow_step=self.wf_step,
            status=terminal_status,
        )

        step_run, should_execute = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )

        assert should_execute is False
        assert step_run.id == existing.id
        # Status should not have changed
        step_run.refresh_from_db()
        assert step_run.status == terminal_status

    def test_running_step_resets_timing(self):
        """A RUNNING step (crash recovery) gets started_at reset."""
        old_time = timezone.now() - timezone.timedelta(minutes=5)
        existing = ValidationStepRunFactory(
            validation_run=self.run,
            workflow_step=self.wf_step,
            status=StepStatus.RUNNING,
            started_at=old_time,
        )

        step_run, should_execute = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )

        assert should_execute is True
        assert step_run.id == existing.id
        step_run.refresh_from_db()
        # started_at should be reset to a recent time, not the old one
        assert step_run.started_at > old_time

    def test_running_step_clears_partial_findings(self):
        """A RUNNING step (crash recovery) has its findings cleared."""
        existing = ValidationStepRunFactory(
            validation_run=self.run,
            workflow_step=self.wf_step,
            status=StepStatus.RUNNING,
        )
        # Simulate partial findings from a crashed prior attempt
        ValidationFinding.objects.create(
            validation_run=self.run,
            validation_step_run=existing,
            severity=Severity.ERROR,
            message="stale finding from crashed attempt",
        )
        assert (
            ValidationFinding.objects.filter(
                validation_step_run=existing,
            ).count()
            == 1
        )

        step_run, should_execute = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )

        assert should_execute is True
        # Partial findings should be cleared
        assert (
            ValidationFinding.objects.filter(
                validation_step_run=step_run,
            ).count()
            == 0
        )

    def test_idempotent_on_second_new_call(self):
        """Two calls for the same (run, step) return the same step run."""
        step_run_1, _ = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )
        step_run_2, _ = self.orchestrator._start_step_run(
            validation_run=self.run,
            workflow_step=self.wf_step,
        )

        assert step_run_1.id == step_run_2.id
        # Only one row should exist
        assert (
            ValidationStepRun.objects.filter(
                validation_run=self.run,
                workflow_step=self.wf_step,
            ).count()
            == 1
        )
