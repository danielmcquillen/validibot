from __future__ import annotations

import pytest

from django.test import Client

from simplevalidations.events.constants import AppEventType
from simplevalidations.tracking.models import TrackingEvent
from simplevalidations.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_login_emits_tracking_event(client: Client):
    password = "TestPass!234"
    user = UserFactory(password=password)

    assert client.login(username=user.username, password=password)

    event = TrackingEvent.objects.filter(
        app_event_type=AppEventType.USER_LOGGED_IN,
    ).first()

    assert event is not None
    assert event.user_id == user.id


@pytest.mark.django_db
def test_logout_emits_tracking_event(client: Client):
    password = "LogOutPass!234"
    user = UserFactory(password=password)

    assert client.login(username=user.username, password=password)
    client.logout()

    event = TrackingEvent.objects.filter(
        app_event_type=AppEventType.USER_LOGGED_OUT,
    ).first()

    assert event is not None
