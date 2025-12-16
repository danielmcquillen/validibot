from __future__ import annotations

import pytest

from validibot.events.constants import AppEventType
from validibot.tracking.models import TrackingEvent
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.engines.base import ValidationResult
from validibot.validations.services.validation_run import ValidationRunService
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


@pytest.mark.django_db
def test_execute_logs_started_and_success(monkeypatch):
    run = ValidationRunFactory()
    WorkflowStepFactory(workflow=run.workflow)
    TrackingEvent.objects.all().delete()

    def success_step(self, step, validation_run):
        return ValidationResult(passed=True, issues=[])

    monkeypatch.setattr(
        ValidationRunService,
        "execute_workflow_step",
        success_step,
    )

    service = ValidationRunService()
    actor_id = run.user_id or getattr(run.submission, "user_id", None)

    service.execute(validation_run_id=run.id, user_id=actor_id)

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
    run = ValidationRunFactory()
    failing_step = WorkflowStepFactory(workflow=run.workflow)
    TrackingEvent.objects.all().delete()

    def failure_step(self, step, validation_run):
        return ValidationResult(passed=False, issues=["boom"])

    monkeypatch.setattr(
        ValidationRunService,
        "execute_workflow_step",
        failure_step,
    )

    service = ValidationRunService()
    actor_id = run.user_id or getattr(run.submission, "user_id", None)

    service.execute(validation_run_id=run.id, user_id=actor_id)

    failure_event = TrackingEvent.objects.filter(
        app_event_type=AppEventType.VALIDATION_RUN_FAILED,
    ).first()
    assert failure_event is not None
    assert failure_event.extra_data.get("failing_step_id") == failing_step.id
