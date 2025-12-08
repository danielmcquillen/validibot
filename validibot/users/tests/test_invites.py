from datetime import timedelta

import pytest
from django.utils import timezone

from validibot.users.constants import RoleCode
from validibot.users.models import PendingInvite
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_pending_invite_accept_creates_membership():
    inviter = UserFactory()
    org = OrganizationFactory()
    inviter_membership, _ = inviter.memberships.get_or_create(
        org=org,
        defaults={"is_active": True},
    )
    inviter_membership.set_roles({RoleCode.ADMIN})
    invitee = UserFactory()
    invite = PendingInvite.create_with_expiry(
        org=org,
        inviter=inviter,
        invitee_user=invitee,
        invitee_email=invitee.email,
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() + timedelta(days=1),
    )

    membership = invite.accept()

    assert invite.status == PendingInvite.Status.ACCEPTED
    assert membership.org == org
    assert membership.user == invitee
    assert RoleCode.WORKFLOW_VIEWER in membership.role_codes


@pytest.mark.django_db
def test_pending_invite_expires_and_cannot_accept():
    inviter = UserFactory()
    org = OrganizationFactory()
    invitee = UserFactory()
    invite = PendingInvite.create_with_expiry(
        org=org,
        inviter=inviter,
        invitee_user=invitee,
        invitee_email=invitee.email,
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() - timedelta(days=1),
    )

    invite.mark_expired_if_needed()
    assert invite.status == PendingInvite.Status.EXPIRED
