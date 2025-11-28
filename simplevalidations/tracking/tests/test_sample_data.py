from __future__ import annotations

import pytest

from simplevalidations.events.constants import AppEventType
from simplevalidations.projects.tests.factories import ProjectFactory
from simplevalidations.tracking.models import TrackingEvent
from simplevalidations.tracking.sample_data import seed_sample_tracking_data
from simplevalidations.users.tests.factories import OrganizationFactory
from simplevalidations.users.tests.factories import UserFactory
from simplevalidations.workflows.tests.factories import WorkflowFactory


@pytest.mark.django_db
def test_seed_sample_tracking_data_creates_events():
    org = OrganizationFactory()
    user = UserFactory(orgs=[org])
    project = ProjectFactory(org=org)
    workflow = WorkflowFactory(org=org, user=user)

    TrackingEvent.objects.all().delete()

    events = seed_sample_tracking_data(
        org=org,
        project=project,
        workflow=workflow,
        user=user,
        days=2,
        runs_per_day=1,
        logins_per_day=1,
    )

    assert len(events) == TrackingEvent.objects.count()
    events = TrackingEvent.objects.filter(app_event_type=AppEventType.USER_LOGGED_IN)
    assert events.exists()
    assert TrackingEvent.objects.filter(
        app_event_type=AppEventType.VALIDATION_RUN_SUCCEEDED,
    ).exists()
