from __future__ import annotations

import pytest

from validibot.events.constants import AppEventType
from validibot.projects.tests.factories import ProjectFactory
from validibot.tracking.models import TrackingEvent
from validibot.tracking.sample_data import seed_sample_tracking_data
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.workflows.tests.factories import WorkflowFactory


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
