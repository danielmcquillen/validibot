from datetime import timedelta

import pytest
from django.utils import timezone

from validibot.core.constants import InviteStatus
from validibot.users.constants import RoleCode
from validibot.users.models import MemberInvite
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_member_invite_accept_creates_membership():
    """Test that accepting a member invite creates a membership."""
    inviter = UserFactory()
    org = OrganizationFactory()
    inviter_membership, _ = inviter.memberships.get_or_create(
        org=org,
        defaults={"is_active": True},
    )
    inviter_membership.set_roles({RoleCode.ADMIN})
    invitee = UserFactory()
    invite = MemberInvite.create_with_expiry(
        org=org,
        inviter=inviter,
        invitee_user=invitee,
        invitee_email=invitee.email,
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() + timedelta(days=1),
    )

    membership = invite.accept()

    assert invite.status == InviteStatus.ACCEPTED
    assert membership.org == org
    assert membership.user == invitee
    assert RoleCode.WORKFLOW_VIEWER in membership.role_codes


@pytest.mark.django_db
def test_member_invite_expires_and_cannot_accept():
    """Test that expired member invites cannot be accepted."""
    inviter = UserFactory()
    org = OrganizationFactory()
    invitee = UserFactory()
    invite = MemberInvite.create_with_expiry(
        org=org,
        inviter=inviter,
        invitee_user=invitee,
        invitee_email=invitee.email,
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() - timedelta(days=1),
    )

    invite.mark_expired_if_needed()
    assert invite.status == InviteStatus.EXPIRED
