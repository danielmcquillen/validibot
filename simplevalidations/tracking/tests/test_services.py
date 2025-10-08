from __future__ import annotations

import pytest

from simplevalidations.events.constants import AppEventType
from simplevalidations.tracking.models import TrackingEvent
from simplevalidations.tracking.services import TrackingEventService
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.tests.factories import ValidationRunFactory


@pytest.mark.django_db
def test_log_validation_run_created_records_event():
    run = ValidationRunFactory()
    service = TrackingEventService()

    event = service.log_validation_run_created(run=run)

    assert event is not None
    assert event.app_event_type == AppEventType.VALIDATION_RUN_CREATED
    assert event.org_id == run.org_id
    assert event.extra_data.get("validation_run_id") == str(run.id)


@pytest.mark.django_db
def test_log_validation_run_status_maps_events():
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
