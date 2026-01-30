from __future__ import annotations

from collections import Counter

import pytest

from validibot.events.constants import AppEventType
from validibot.tracking.models import TrackingEvent
from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.services.validation_run import ValidationRunService
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


@pytest.mark.django_db
def test_execute_logs_started_and_success(monkeypatch):
    """Test that validation run tracking events are logged correctly on success."""
    run = ValidationRunFactory()
    WorkflowStepFactory(workflow=run.workflow)
    TrackingEvent.objects.all().delete()

    def mock_execute_validator_step(self, *, validation_run, step_run):
        # Mark step as passed (processor normally does this)
        step_run.status = StepStatus.PASSED
        step_run.save()
        return {
            "step_run": step_run,
            "severity_counts": Counter(),
            "total_findings": 0,
            "assertion_failures": 0,
            "assertion_total": 0,
            "passed": True,
        }

    monkeypatch.setattr(
        ValidationRunService,
        "_execute_validator_step",
        mock_execute_validator_step,
    )

    service = ValidationRunService()
    actor_id = run.user_id or getattr(run.submission, "user_id", None)

    service.execute_workflow_steps(validation_run_id=run.id, user_id=actor_id)

    started_event = TrackingEvent.objects.filter(
        app_event_type=AppEventType.VALIDATION_RUN_STARTED,
    ).first()
    assert started_event is not None
    assert started_event.extra_data.get("status") == ValidationRunStatus.RUNNING

    success_event = TrackingEvent.objects.filter(
        app_event_type=AppEventType.VALIDATION_RUN_SUCCEEDED,
    ).first()
    assert success_event is not None
    assert success_event.extra_data.get("step_count") == 1


@pytest.mark.django_db
def test_execute_logs_failure(monkeypatch):
    """Test that validation run tracking events are logged correctly on failure."""
    run = ValidationRunFactory()
    failing_step = WorkflowStepFactory(workflow=run.workflow)
    TrackingEvent.objects.all().delete()

    def mock_execute_validator_step(self, *, validation_run, step_run):
        # Mark step as failed (processor normally does this)
        step_run.status = StepStatus.FAILED
        step_run.save()
        return {
            "step_run": step_run,
            "severity_counts": Counter({Severity.ERROR: 1}),
            "total_findings": 1,
            "assertion_failures": 0,
            "assertion_total": 0,
            "passed": False,
        }

    monkeypatch.setattr(
        ValidationRunService,
        "_execute_validator_step",
        mock_execute_validator_step,
    )

    service = ValidationRunService()
    actor_id = run.user_id or getattr(run.submission, "user_id", None)

    service.execute_workflow_steps(validation_run_id=run.id, user_id=actor_id)

    failure_event = TrackingEvent.objects.filter(
        app_event_type=AppEventType.VALIDATION_RUN_FAILED,
    ).first()
    assert failure_event is not None
    assert failure_event.extra_data.get("failing_step_id") == failing_step.id
