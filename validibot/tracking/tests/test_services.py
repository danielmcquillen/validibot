"""Tests for durable product-tracking event metadata.

Validation lifecycle analytics must retain stable run identifiers and, once an
execution attempt exists, enough immutable route data to compare Service and
Job behavior without parsing provider resource names.
"""

from __future__ import annotations

import pytest

from validibot.events.constants import AppEventType
from validibot.tracking.models import TrackingEvent
from validibot.tracking.services import TrackingEventService
from validibot.validations.constants import ExecutionDeploymentKind
from validibot.validations.constants import ExecutionProviderType
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.tests.factories import ExecutionAttemptFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory


@pytest.mark.django_db
def test_log_validation_run_created_records_event():
    """Creation analytics should retain the run and tenant association."""
    run = ValidationRunFactory()
    service = TrackingEventService()

    event = service.log_validation_run_created(run=run)

    assert event is not None
    assert event.app_event_type == AppEventType.VALIDATION_RUN_CREATED
    assert event.org_id == run.org_id
    assert event.extra_data.get("validation_run_id") == str(run.id)


@pytest.mark.django_db
def test_log_validation_run_status_maps_events():
    """Terminal statuses should map to the stable application event vocabulary."""
    run = ValidationRunFactory()
    service = TrackingEventService()

    service.log_validation_run_status(
        run=run,
        status=ValidationRunStatus.SUCCEEDED,
    )

    event = TrackingEvent.objects.filter(
        app_event_type=AppEventType.VALIDATION_RUN_SUCCEEDED,
    ).first()

    assert event is not None
    assert event.extra_data.get("status") == ValidationRunStatus.SUCCEEDED


@pytest.mark.django_db
def test_terminal_tracking_event_records_service_or_job_route():
    """Analytics must distinguish the selected provider primitive explicitly."""
    run = ValidationRunFactory()
    step_run = ValidationStepRunFactory(validation_run=run)
    ExecutionAttemptFactory(
        step_run=step_run,
        runner_type="CloudRunServiceExecutionBackend",
        deployment_snapshot={
            "provider_type": ExecutionProviderType.GCP,
            "deployment_kind": ExecutionDeploymentKind.CLOUD_RUN_SERVICE,
        },
    )

    event = TrackingEventService().log_validation_run_status(
        run=run,
        status=ValidationRunStatus.SUCCEEDED,
    )

    assert event is not None
    assert event.extra_data["execution_runner_type"] == (
        "CloudRunServiceExecutionBackend"
    )
    assert event.extra_data["execution_provider_type"] == ExecutionProviderType.GCP
    assert event.extra_data["execution_deployment_kind"] == (
        ExecutionDeploymentKind.CLOUD_RUN_SERVICE
    )
    assert event.extra_data["execution_deployment_kinds"] == [
        ExecutionDeploymentKind.CLOUD_RUN_SERVICE
    ]
