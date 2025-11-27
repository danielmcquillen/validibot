from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from simplevalidations.notifications.models import Notification
from simplevalidations.tracking.models import TrackingEvent
from simplevalidations.events.constants import AppEventType
from simplevalidations.tracking.constants import TrackingEventType
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.models import PendingInvite
from simplevalidations.users.tests.factories import OrganizationFactory, UserFactory


@pytest.mark.django_db
def test_invite_notification_shows_for_invitee(client):
    inviter = UserFactory()
    org = inviter.orgs.first()
    inviter.memberships.get(org=org).set_roles({RoleCode.ADMIN})
    invitee = UserFactory()
    inviter.current_org = org
    inviter.save(update_fields=["current_org"])

    client.force_login(inviter)
    session = client.session
    session["active_org_id"] = org.id
    session.save()
    response = client.post(
        reverse("members:invite_create"),
        {
            "search": invitee.username,
            "invitee_user": invitee.id,
            "invitee_email": invitee.email,
            "roles": [RoleCode.WORKFLOW_VIEWER],
        },
        follow=True,
    )
    assert response.status_code == 200
    assert PendingInvite.objects.filter(invitee_user=invitee, org=org).exists()
    assert Notification.objects.filter(user=invitee).count() == 1
    event = TrackingEvent.objects.filter(app_event_type=AppEventType.INVITE_CREATED).first()
    assert event is not None
    assert event.event_type == TrackingEventType.APP_EVENT
    assert event.org_id == org.id
    assert event.user_id == inviter.id
    assert event.extra_data.get("invitee_user_id") == invitee.id

    client.force_login(invitee)
    resp = client.get(reverse("notifications:notification-list"))
    assert resp.status_code == 200
    content = resp.content.decode()
    assert org.name in content
    assert "Invitation" in content


@pytest.mark.django_db
def test_invitee_can_accept_and_become_member(client):
    inviter = UserFactory()
    org = inviter.orgs.first()
    inviter.memberships.get(org=org).set_roles({RoleCode.ADMIN})
    invitee = UserFactory()

    invite = PendingInvite.create_with_expiry(
        org=org,
        inviter=inviter,
        invitee_user=invitee,
        invitee_email=invitee.email,
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() + timedelta(days=1),
    )
    notification = Notification.objects.create(
        user=invitee,
        org=org,
        type=Notification.Type.INVITE,
        invite=invite,
        payload={"roles": invite.roles},
    )

    client.force_login(invitee)
    session = client.session
    session["active_org_id"] = org.id
    session.save()
    resp = client.post(
        reverse("notifications:notification-invite-accept", kwargs={"pk": notification.pk})
    )
    assert resp.status_code in (302, 200)
    invite.refresh_from_db()
    assert invite.status == PendingInvite.Status.ACCEPTED
    membership = invitee.memberships.filter(org=org).first()
    assert membership is not None
    assert RoleCode.WORKFLOW_VIEWER in membership.role_codes
    event = TrackingEvent.objects.filter(app_event_type=AppEventType.INVITE_ACCEPTED).last()
    assert event is not None
    assert event.event_type == TrackingEventType.APP_EVENT
    assert event.org_id == org.id
    assert event.user_id == invitee.id


@pytest.mark.django_db
def test_dismissing_notification_sets_timestamp(client):
    user = UserFactory()
    org = user.orgs.first()
    notification = Notification.objects.create(
        user=user,
        org=org,
        type=Notification.Type.SYSTEM_ALERT,
        payload={"message": "Hello"},
    )

    client.force_login(user)
    session = client.session
    session["active_org_id"] = org.id
    session.save()

    resp = client.post(
        reverse("notifications:notification-dismiss", kwargs={"pk": notification.pk}),
        HTTP_HX_REQUEST="true",
    )
    assert resp.status_code == 200
    notification.refresh_from_db()
    assert notification.dismissed_at is not None


@pytest.mark.django_db
def test_invitee_can_decline_and_event_is_logged(client):
    inviter = UserFactory()
    org = inviter.orgs.first()
    inviter.memberships.get(org=org).set_roles({RoleCode.ADMIN})
    invitee = UserFactory()

    invite = PendingInvite.create_with_expiry(
        org=org,
        inviter=inviter,
        invitee_user=invitee,
        invitee_email=invitee.email,
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() + timedelta(days=1),
    )
    notification = Notification.objects.create(
        user=invitee,
        org=org,
        type=Notification.Type.INVITE,
        invite=invite,
        payload={"roles": invite.roles},
    )

    client.force_login(invitee)
    session = client.session
    session["active_org_id"] = org.id
    session.save()
    resp = client.post(
        reverse("notifications:notification-invite-decline", kwargs={"pk": notification.pk})
    )
    assert resp.status_code in (302, 200)
    invite.refresh_from_db()
    assert invite.status == PendingInvite.Status.DECLINED
    event = TrackingEvent.objects.filter(app_event_type=AppEventType.INVITE_DECLINED).last()
    assert event is not None
    assert event.event_type == TrackingEventType.APP_EVENT
    assert event.org_id == org.id
    assert event.user_id == invitee.id


@pytest.mark.django_db
def test_can_dismiss_rules_for_invitee_and_inviter():
    inviter = UserFactory()
    invitee = UserFactory()
    org = inviter.orgs.first()
    inviter.memberships.get(org=org).set_roles({RoleCode.ADMIN})
    invite = PendingInvite.create_with_expiry(
        org=org,
        inviter=inviter,
        invitee_user=invitee,
        invitee_email=invitee.email,
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() + timedelta(days=1),
    )
    invitee_notification = Notification.objects.create(
        user=invitee,
        org=org,
        type=Notification.Type.INVITE,
        invite=invite,
        payload={},
    )
    inviter_notification = Notification.objects.create(
        user=inviter,
        org=org,
        type=Notification.Type.INVITE,
        invite=invite,
        payload={},
    )

    assert invitee_notification.can_dismiss is False  # pending invitee cannot dismiss
    assert inviter_notification.can_dismiss is True  # inviter can always dismiss

    invite.decline()
    invitee_notification.refresh_from_db()
    assert invitee_notification.can_dismiss is True  # once resolved, invitee can dismiss
