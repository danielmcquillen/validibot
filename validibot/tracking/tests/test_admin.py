"""Tests for the read-only tracking-event Django admin.

Tracking events are operational evidence used by dashboards and diagnostics.
This suite ensures operators can find and inspect those rows while Django admin
cannot become an alternate write path that corrupts historical analytics.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from django.contrib import admin
from django.urls import reverse

from validibot.tracking.admin import TrackingEventAdmin
from validibot.tracking.models import TrackingEvent
from validibot.tracking.tests.factories import TrackingEventFactory

pytestmark = pytest.mark.django_db


class TestTrackingEventAdmin:
    """Pin registration, discoverability, and immutability of event history."""

    def test_tracking_event_is_registered(self):
        """The admin index must expose tracking events to authorized operators."""
        model_admin = admin.site._registry.get(TrackingEvent)

        assert isinstance(model_admin, TrackingEventAdmin)

    def test_changelist_supports_operator_search(self, admin_client):
        """Operators must be able to locate an event by organization context."""
        event = TrackingEventFactory()
        url = reverse("admin:tracking_trackingevent_changelist")

        response = admin_client.get(url, {"q": event.org.slug})

        assert response.status_code == HTTPStatus.OK
        assert str(event.pk) in response.content.decode()

    def test_detail_page_is_viewable_and_has_no_save_controls(self, admin_client):
        """Drill-in should expose event metadata without presenting edit actions."""
        event = TrackingEventFactory(extra_data={"source": "admin-test"})
        url = reverse(
            "admin:tracking_trackingevent_change",
            kwargs={"object_id": event.pk},
        )

        response = admin_client.get(url)
        content = response.content.decode()

        assert response.status_code == HTTPStatus.OK
        assert "admin-test" in content
        assert 'name="_save"' not in content
        assert 'name="_continue"' not in content
        assert 'name="_addanother"' not in content

    def test_all_mutation_permissions_are_disabled(self, rf, admin_user):
        """Even a superuser must not manufacture, rewrite, or delete telemetry."""
        request = rf.get("/")
        request.user = admin_user
        model_admin = admin.site._registry[TrackingEvent]
        event = TrackingEventFactory()

        assert model_admin.has_view_permission(request, event)
        assert not model_admin.has_add_permission(request)
        assert not model_admin.has_change_permission(request, event)
        assert not model_admin.has_delete_permission(request, event)
