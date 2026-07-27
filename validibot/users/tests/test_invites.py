"""Tests for member-invitation acceptance, expiry, and access transitions.

Invitation acceptance changes the user's organization authority, so the suite
also verifies that prior guest grants are fully deactivated and cannot
resurface after a later membership removal.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from validibot.core.constants import InviteStatus
from validibot.users.constants import RoleCode
from validibot.users.models import MemberInvite
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.workflows.models import OrgGuestAccess
from validibot.workflows.models import WorkflowAccessGrant
from validibot.workflows.tests.factories import WorkflowFactory


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


@pytest.mark.django_db
def test_member_invite_accept_deactivates_all_guest_access():
    """Promotion to member must prevent old guest access from resurfacing later."""

    inviter = UserFactory()
    invitee = UserFactory()
    org = OrganizationFactory()
    workflow = WorkflowFactory(org=org, user=inviter)
    workflow_grant = WorkflowAccessGrant.objects.create(
        workflow=workflow,
        user=invitee,
        granted_by=inviter,
        is_active=True,
    )
    org_grant = OrgGuestAccess.objects.create(
        user=invitee,
        org=org,
        granted_by=inviter,
        is_active=True,
    )
    invite = MemberInvite.create_with_expiry(
        org=org,
        inviter=inviter,
        invitee_user=invitee,
        invitee_email=invitee.email,
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() + timedelta(days=1),
    )

    membership = invite.accept()

    workflow_grant.refresh_from_db()
    org_grant.refresh_from_db()
    assert membership.is_active is True
    assert workflow_grant.is_active is False
    assert org_grant.is_active is False

    membership.delete()
    assert not OrgGuestAccess.objects.filter(
        user=invitee,
        org=org,
        is_active=True,
    ).exists()
