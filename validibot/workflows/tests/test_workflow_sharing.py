"""
Tests for the workflow sharing feature.

This module tests WorkflowAccessGrant, WorkflowInvite models,
and the associated permission extensions for cross-organization
workflow sharing.
"""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
from unittest.mock import patch

import pytest
from django.urls import reverse
from django.utils import timezone

from validibot.users.constants import RoleCode
from validibot.users.models import Membership
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.workflows.models import GuestInvite
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowAccessGrant
from validibot.workflows.models import WorkflowInvite
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


# =============================================================================
# WorkflowAccessGrant Model Tests
# =============================================================================


class TestWorkflowAccessGrant:
    """Tests for the WorkflowAccessGrant model."""

    def test_create_access_grant(self):
        """Test creating a basic access grant."""
        workflow = WorkflowFactory()
        user = UserFactory(orgs=[])  # User with no org membership
        granter = workflow.user

        grant = WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=user,
            granted_by=granter,
            is_active=True,
        )

        assert grant.workflow == workflow
        assert grant.user == user
        assert grant.granted_by == granter
        assert grant.is_active is True
        assert str(grant) == f"{user} -> {workflow.name}"

    def test_unique_constraint_workflow_user(self):
        """Test that workflow+user combination must be unique."""
        workflow = WorkflowFactory()
        user = UserFactory(orgs=[])

        WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=user,
            is_active=True,
        )

        from django.db import IntegrityError

        with pytest.raises(IntegrityError):
            WorkflowAccessGrant.objects.create(
                workflow=workflow,
                user=user,
                is_active=True,
            )

    def test_revoke_grant(self):
        """Test revoking an access grant."""
        workflow = WorkflowFactory()
        user = UserFactory(orgs=[])

        grant = WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=user,
            is_active=True,
        )

        grant.revoke()
        grant.refresh_from_db()

        assert grant.is_active is False

    def test_grant_with_notes(self):
        """Test creating a grant with notes."""
        workflow = WorkflowFactory()
        user = UserFactory(orgs=[])

        grant = WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=user,
            notes="Granted for external review",
        )

        assert grant.notes == "Granted for external review"


# =============================================================================
# WorkflowInvite Model Tests
# =============================================================================


class TestWorkflowInvite:
    """Tests for the WorkflowInvite model."""

    def test_create_invite_with_expiry(self):
        """Test creating an invite with default expiry."""
        workflow = WorkflowFactory()
        inviter = workflow.user

        with patch(
            "validibot.workflows.emails.send_workflow_invite_email",
        ) as mock_send:
            mock_send.return_value = True
            invite = WorkflowInvite.create_with_expiry(
                workflow=workflow,
                inviter=inviter,
                invitee_email="guest@example.com",
            )

        assert invite.workflow == workflow
        assert invite.inviter == inviter
        assert invite.invitee_email == "guest@example.com"
        assert invite.status == WorkflowInvite.Status.PENDING
        assert invite.token is not None
        assert invite.expires_at > timezone.now()
        mock_send.assert_called_once_with(invite)

    def test_create_invite_custom_expiry(self):
        """Test creating an invite with custom expiry."""
        workflow = WorkflowFactory()
        inviter = workflow.user

        with patch(
            "validibot.workflows.emails.send_workflow_invite_email",
        ) as mock_send:
            mock_send.return_value = True
            invite = WorkflowInvite.create_with_expiry(
                workflow=workflow,
                inviter=inviter,
                invitee_email="guest@example.com",
                expiry_days=30,
            )

        expected_expiry = timezone.now() + timedelta(days=30)
        tolerance_seconds = 5
        time_diff = abs((invite.expires_at - expected_expiry).total_seconds())
        assert time_diff < tolerance_seconds

    def test_create_invite_without_email(self):
        """Test creating an invite without sending email."""
        workflow = WorkflowFactory()
        inviter = workflow.user

        with patch(
            "validibot.workflows.emails.send_workflow_invite_email",
        ) as mock_send:
            invite = WorkflowInvite.create_with_expiry(
                workflow=workflow,
                inviter=inviter,
                invitee_email="guest@example.com",
                send_email=False,
            )

        assert invite.invitee_email == "guest@example.com"
        mock_send.assert_not_called()

    def test_accept_invite_creates_grant(self):
        """Test that accepting an invite creates a WorkflowAccessGrant."""
        workflow = WorkflowFactory()
        inviter = workflow.user
        invitee = UserFactory(orgs=[])

        invite = WorkflowInvite.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_email=invitee.email,
            expires_at=timezone.now() + timedelta(days=7),
        )

        grant = invite.accept(user=invitee)

        assert grant.workflow == workflow
        assert grant.user == invitee
        assert grant.granted_by == inviter
        assert grant.is_active is True
        invite.refresh_from_db()
        assert invite.status == WorkflowInvite.Status.ACCEPTED
        assert invite.invitee_user == invitee

    def test_accept_invite_reactivates_inactive_grant(self):
        """Test accepting invite reactivates an existing inactive grant."""
        workflow = WorkflowFactory()
        inviter = workflow.user
        invitee = UserFactory(orgs=[])

        # Create an inactive grant
        existing_grant = WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=invitee,
            is_active=False,
        )

        invite = WorkflowInvite.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_user=invitee,
            expires_at=timezone.now() + timedelta(days=7),
        )

        grant = invite.accept()

        assert grant.pk == existing_grant.pk
        grant.refresh_from_db()
        assert grant.is_active is True
        assert grant.granted_by == inviter

    def test_accept_invite_uses_invitee_user_if_no_user_provided(self):
        """Test accepting invite uses invitee_user if no user argument provided."""
        workflow = WorkflowFactory()
        inviter = workflow.user
        invitee = UserFactory(orgs=[])

        invite = WorkflowInvite.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_user=invitee,
            expires_at=timezone.now() + timedelta(days=7),
        )

        grant = invite.accept()

        assert grant.user == invitee

    def test_accept_invite_requires_user(self):
        """Test that accepting without a user raises ValueError."""
        workflow = WorkflowFactory()
        inviter = workflow.user

        invite = WorkflowInvite.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_email="guest@example.com",
            expires_at=timezone.now() + timedelta(days=7),
        )

        with pytest.raises(ValueError, match="No user provided"):
            invite.accept()

    def test_cannot_accept_non_pending_invite(self):
        """Test that only pending invites can be accepted."""
        workflow = WorkflowFactory()
        inviter = workflow.user
        invitee = UserFactory(orgs=[])

        for status in [
            WorkflowInvite.Status.ACCEPTED,
            WorkflowInvite.Status.DECLINED,
            WorkflowInvite.Status.CANCELED,
            WorkflowInvite.Status.EXPIRED,
        ]:
            invite = WorkflowInvite.objects.create(
                workflow=workflow,
                inviter=inviter,
                invitee_user=invitee,
                status=status,
                expires_at=timezone.now() + timedelta(days=7),
            )

            with pytest.raises(ValueError, match="Cannot accept invite"):
                invite.accept()

    def test_accept_expired_invite_raises_error(self):
        """Test that accepting an expired invite raises ValueError."""
        workflow = WorkflowFactory()
        inviter = workflow.user
        invitee = UserFactory(orgs=[])

        invite = WorkflowInvite.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_user=invitee,
            expires_at=timezone.now() - timedelta(hours=1),
        )

        with pytest.raises(ValueError, match="Invite has expired"):
            invite.accept()

    def test_decline_invite(self):
        """Test declining an invite."""
        workflow = WorkflowFactory()
        inviter = workflow.user
        invitee = UserFactory(orgs=[])

        invite = WorkflowInvite.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_user=invitee,
            expires_at=timezone.now() + timedelta(days=7),
        )

        invite.decline()
        invite.refresh_from_db()

        assert invite.status == WorkflowInvite.Status.DECLINED

    def test_cancel_invite(self):
        """Test canceling an invite."""
        workflow = WorkflowFactory()
        inviter = workflow.user

        invite = WorkflowInvite.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_email="guest@example.com",
            expires_at=timezone.now() + timedelta(days=7),
        )

        invite.cancel()
        invite.refresh_from_db()

        assert invite.status == WorkflowInvite.Status.CANCELED

    def test_mark_expired_if_needed(self):
        """Test marking an expired invite."""
        workflow = WorkflowFactory()
        inviter = workflow.user

        invite = WorkflowInvite.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_email="guest@example.com",
            expires_at=timezone.now() - timedelta(hours=1),
        )

        assert invite.mark_expired_if_needed() is True
        invite.refresh_from_db()
        assert invite.status == WorkflowInvite.Status.EXPIRED

    def test_mark_expired_if_needed_returns_false_when_not_expired(self):
        """Test that mark_expired_if_needed returns False when not expired."""
        workflow = WorkflowFactory()
        inviter = workflow.user

        invite = WorkflowInvite.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_email="guest@example.com",
            expires_at=timezone.now() + timedelta(days=7),
        )

        assert invite.mark_expired_if_needed() is False
        assert invite.status == WorkflowInvite.Status.PENDING

    def test_is_pending_property(self):
        """Test the is_pending property."""
        workflow = WorkflowFactory()
        inviter = workflow.user

        # Pending and not expired
        invite = WorkflowInvite.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_email="guest@example.com",
            expires_at=timezone.now() + timedelta(days=7),
        )
        assert invite.is_pending is True

        # Pending but expired
        invite.expires_at = timezone.now() - timedelta(hours=1)
        invite.save()
        assert invite.is_pending is False

    def test_is_expired_property(self):
        """Test the is_expired property."""
        workflow = WorkflowFactory()
        inviter = workflow.user

        # Not expired
        invite = WorkflowInvite.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_email="guest@example.com",
            expires_at=timezone.now() + timedelta(days=7),
        )
        assert invite.is_expired is False

        # Expired by time
        invite.expires_at = timezone.now() - timedelta(hours=1)
        invite.save()
        assert invite.is_expired is True

        # Expired by status
        invite.expires_at = timezone.now() + timedelta(days=7)
        invite.status = WorkflowInvite.Status.EXPIRED
        invite.save()
        assert invite.is_expired is True


# =============================================================================
# User.is_workflow_guest Property Tests
# =============================================================================


class TestIsWorkflowGuest:
    """Tests for the User.is_workflow_guest property."""

    def test_user_with_no_memberships_and_grants_is_guest(self):
        """Test that user with grants but no memberships is a guest."""
        user = UserFactory(orgs=[])
        # Remove any auto-created memberships
        Membership.objects.filter(user=user).delete()

        workflow = WorkflowFactory()
        WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=user,
            is_active=True,
        )

        assert user.is_workflow_guest is True

    def test_user_with_membership_is_not_guest(self):
        """Test that user with active membership is not a guest."""
        org = OrganizationFactory()
        user = UserFactory(orgs=[])
        grant_role(user, org, RoleCode.EXECUTOR)

        workflow = WorkflowFactory()
        WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=user,
            is_active=True,
        )

        assert user.is_workflow_guest is False

    def test_user_with_inactive_membership_and_grant_is_guest(self):
        """Test that user with inactive membership but active grant is guest."""
        org = OrganizationFactory()
        user = UserFactory(orgs=[])
        # Delete all auto-created memberships
        Membership.objects.filter(user=user).delete()

        # Create only an inactive membership
        membership = Membership.objects.create(
            user=user,
            org=org,
            is_active=False,
        )
        assert membership.is_active is False

        workflow = WorkflowFactory()
        WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=user,
            is_active=True,
        )

        assert user.is_workflow_guest is True

    def test_user_with_no_grants_is_not_guest(self):
        """Test that user without any grants is not a guest."""
        user = UserFactory(orgs=[])
        Membership.objects.filter(user=user).delete()

        assert user.is_workflow_guest is False

    def test_user_with_inactive_grant_is_not_guest(self):
        """Test that user with only inactive grants is not a guest."""
        user = UserFactory(orgs=[])
        Membership.objects.filter(user=user).delete()

        workflow = WorkflowFactory()
        WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=user,
            is_active=False,
        )

        assert user.is_workflow_guest is False


# =============================================================================
# Workflow Permission Extension Tests
# =============================================================================


class TestWorkflowPermissionExtensions:
    """Tests for extended Workflow permissions (can_view, can_execute, for_user)."""

    def test_can_view_with_grant(self):
        """Test that users with grants can view workflows."""
        workflow = WorkflowFactory(is_active=True)
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()

        # Without grant, cannot view
        assert workflow.can_view(user=guest) is False

        # With grant, can view
        WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=guest,
            is_active=True,
        )
        assert workflow.can_view(user=guest) is True

    def test_can_view_with_inactive_grant(self):
        """Test that inactive grants don't allow viewing."""
        workflow = WorkflowFactory(is_active=True)
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()

        WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=guest,
            is_active=False,
        )

        assert workflow.can_view(user=guest) is False

    def test_can_execute_with_grant(self):
        """Test that users with grants can execute workflows."""
        workflow = WorkflowFactory(is_active=True)
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()

        # Without grant, cannot execute
        assert workflow.can_execute(user=guest) is False

        # With grant, can execute
        WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=guest,
            is_active=True,
        )
        assert workflow.can_execute(user=guest) is True

    def test_can_execute_inactive_workflow(self):
        """Test that grants don't allow execution of inactive workflows."""
        workflow = WorkflowFactory(is_active=False)
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()

        WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=guest,
            is_active=True,
        )

        assert workflow.can_execute(user=guest) is False

    def test_for_user_includes_grant_workflows(self):
        """Test that for_user queryset includes grant-accessible workflows."""
        org = OrganizationFactory()
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()

        workflow1 = WorkflowFactory(org=org, is_active=True)
        workflow2 = WorkflowFactory(org=org, is_active=True)

        # Grant access to workflow1 only
        WorkflowAccessGrant.objects.create(
            workflow=workflow1,
            user=guest,
            is_active=True,
        )

        accessible = Workflow.objects.for_user(guest)

        assert workflow1 in accessible
        assert workflow2 not in accessible

    def test_for_user_with_role_excludes_grants(self):
        """Test that for_user with role code excludes grant-only access."""
        org = OrganizationFactory()
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()

        workflow = WorkflowFactory(org=org, is_active=True)

        WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=guest,
            is_active=True,
        )

        # With role requirement, grants don't count
        accessible = Workflow.objects.for_user(
            guest,
            required_role_code=RoleCode.AUTHOR,
        )

        assert workflow not in accessible

    def test_for_user_combines_membership_and_grants(self):
        """Test that for_user combines membership and grant access."""
        org1 = OrganizationFactory()
        org2 = OrganizationFactory()
        user = UserFactory(orgs=[])
        Membership.objects.filter(user=user).delete()

        # Grant membership to org1
        grant_role(user, org1, RoleCode.EXECUTOR)

        # Workflow in org1 (accessible via membership)
        workflow1 = WorkflowFactory(org=org1, is_active=True)

        # Workflow in org2 (accessible via grant)
        workflow2 = WorkflowFactory(org=org2, is_active=True)
        WorkflowAccessGrant.objects.create(
            workflow=workflow2,
            user=user,
            is_active=True,
        )

        accessible = Workflow.objects.for_user(user)

        assert workflow1 in accessible
        assert workflow2 in accessible


# =============================================================================
# WorkflowInviteAcceptView Tests
# =============================================================================


class TestWorkflowInviteAcceptView:
    """Tests for the WorkflowInviteAcceptView."""

    def test_logged_in_user_accepts_invite_immediately(self, client):
        """Test that logged in user can accept invite immediately."""
        workflow = WorkflowFactory(is_active=True)
        inviter = workflow.user
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()

        invite = WorkflowInvite.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_email=guest.email,
            expires_at=timezone.now() + timedelta(days=7),
        )

        client.force_login(guest)

        with patch(
            "validibot.workflows.emails.send_workflow_invite_accepted_email",
        ) as mock_send:
            mock_send.return_value = True
            url = reverse(
                "workflow_invite_accept",
                kwargs={"token": invite.token},
            )
            response = client.get(url)

        # Should redirect to workflow launch page
        assert response.status_code == HTTPStatus.FOUND
        assert f"/workflows/{workflow.pk}/launch/" in response.url

        # Grant should be created
        assert WorkflowAccessGrant.objects.filter(
            workflow=workflow,
            user=guest,
            is_active=True,
        ).exists()

        # Invite should be accepted
        invite.refresh_from_db()
        assert invite.status == WorkflowInvite.Status.ACCEPTED

        # Email should be sent
        mock_send.assert_called_once()

    def test_anonymous_user_redirected_to_signup(self, client):
        """Test that anonymous user is redirected to signup."""
        workflow = WorkflowFactory(is_active=True)
        inviter = workflow.user

        invite = WorkflowInvite.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_email="newuser@example.com",
            expires_at=timezone.now() + timedelta(days=7),
        )

        response = client.get(
            reverse("workflow_invite_accept", kwargs={"token": invite.token}),
        )

        # Should redirect to signup
        assert response.status_code == HTTPStatus.FOUND
        assert "/accounts/signup/" in response.url

        # Token should be stored in session
        assert client.session.get("workflow_invite_token") == str(invite.token)

        # Invite should still be pending
        invite.refresh_from_db()
        assert invite.status == WorkflowInvite.Status.PENDING

    def test_expired_invite_shows_error(self, client):
        """Test that expired invites show error message."""
        workflow = WorkflowFactory(is_active=True)
        inviter = workflow.user
        guest = UserFactory(orgs=[])

        invite = WorkflowInvite.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_email=guest.email,
            expires_at=timezone.now() - timedelta(hours=1),
        )

        client.force_login(guest)

        response = client.get(
            reverse("workflow_invite_accept", kwargs={"token": invite.token}),
            follow=True,
        )

        # Should redirect to home with error message
        assert response.status_code == HTTPStatus.OK

        # The invite is expired (checked via is_expired property) but
        # status is not persisted on GET — that happens on accept/cancel.
        invite.refresh_from_db()
        assert invite.is_expired

    def test_already_accepted_invite_shows_error(self, client):
        """Test that already accepted invites show error."""
        workflow = WorkflowFactory(is_active=True)
        inviter = workflow.user
        guest = UserFactory(orgs=[])

        invite = WorkflowInvite.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_user=guest,
            status=WorkflowInvite.Status.ACCEPTED,
            expires_at=timezone.now() + timedelta(days=7),
        )

        client.force_login(guest)

        response = client.get(
            reverse("workflow_invite_accept", kwargs={"token": invite.token}),
            follow=True,
        )

        # Should redirect to home with error message
        assert response.status_code == HTTPStatus.OK

    def test_invalid_token_returns_404(self, client):
        """Test that invalid token returns 404."""
        import uuid

        guest = UserFactory(orgs=[])
        client.force_login(guest)

        response = client.get(
            reverse("workflow_invite_accept", kwargs={"token": uuid.uuid4()}),
        )

        assert response.status_code == HTTPStatus.NOT_FOUND


# =============================================================================
# GuestWorkflowListView Tests
# =============================================================================


class TestGuestWorkflowListView:
    """Tests for the GuestWorkflowListView."""

    def test_guest_sees_granted_workflows(self, client):
        """Test that guest user sees workflows they have grants for."""
        org = OrganizationFactory()
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()

        workflow_granted = WorkflowFactory(
            org=org,
            is_active=True,
            name="Granted Workflow",
        )
        WorkflowFactory(org=org, is_active=True, name="Not Granted")

        WorkflowAccessGrant.objects.create(
            workflow=workflow_granted,
            user=guest,
            is_active=True,
        )

        client.force_login(guest)
        response = client.get(reverse("workflows:guest_workflow_list"))

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()
        assert "Granted Workflow" in content
        assert "Not Granted" not in content

    def test_guest_sees_workflows_from_multiple_orgs(self, client):
        """Test that guest sees workflows from all orgs they have grants for."""
        org1 = OrganizationFactory(name="Org Alpha")
        org2 = OrganizationFactory(name="Org Beta")
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()

        workflow1 = WorkflowFactory(org=org1, is_active=True, name="Alpha Workflow")
        workflow2 = WorkflowFactory(org=org2, is_active=True, name="Beta Workflow")

        WorkflowAccessGrant.objects.create(
            workflow=workflow1,
            user=guest,
            is_active=True,
        )
        WorkflowAccessGrant.objects.create(
            workflow=workflow2,
            user=guest,
            is_active=True,
        )

        client.force_login(guest)
        response = client.get(reverse("workflows:guest_workflow_list"))

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()
        assert "Alpha Workflow" in content
        assert "Beta Workflow" in content
        assert "Org Alpha" in content
        assert "Org Beta" in content

    def test_archived_workflows_hidden(self, client):
        """Test that archived workflows are hidden from guest list."""
        org = OrganizationFactory()
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()

        workflow = WorkflowFactory(
            org=org,
            is_active=True,
            is_archived=True,
            name="Archived Workflow",
        )

        WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=guest,
            is_active=True,
        )

        client.force_login(guest)
        response = client.get(reverse("workflows:guest_workflow_list"))

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()
        assert "Archived Workflow" not in content

    def test_inactive_workflows_hidden(self, client):
        """Test that inactive workflows are hidden from guest list."""
        org = OrganizationFactory()
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()

        workflow = WorkflowFactory(
            org=org,
            is_active=False,
            name="Inactive Workflow",
        )

        WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=guest,
            is_active=True,
        )

        client.force_login(guest)
        response = client.get(reverse("workflows:guest_workflow_list"))

        assert response.status_code == HTTPStatus.OK
        content = response.content.decode()
        assert "Inactive Workflow" not in content

    def test_requires_login(self, client):
        """Test that view requires login."""
        response = client.get(reverse("workflows:guest_workflow_list"))

        assert response.status_code == HTTPStatus.FOUND
        assert "/accounts/login/" in response.url


# =============================================================================
# GuestValidationRunListView Tests
# =============================================================================


class TestGuestValidationRunListView:
    """Tests for the GuestValidationRunListView."""

    def test_guest_sees_own_runs_only(self, client):
        """Test that guest sees only their own validation runs."""
        from validibot.submissions.tests.factories import SubmissionFactory
        from validibot.validations.tests.factories import ValidationRunFactory

        org = OrganizationFactory()
        guest = UserFactory(orgs=[])
        other_user = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()

        workflow = WorkflowFactory(org=org, is_active=True)
        WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=guest,
            is_active=True,
        )

        # Guest's submission and run
        guest_submission = SubmissionFactory(
            workflow=workflow,
            org=org,
            user=guest,
        )
        guest_run = ValidationRunFactory(
            submission=guest_submission,
            workflow=workflow,
            org=org,
            user=guest,
        )

        # Other user's run on same workflow
        other_submission = SubmissionFactory(
            workflow=workflow,
            org=org,
            user=other_user,
        )
        other_run = ValidationRunFactory(
            submission=other_submission,
            workflow=workflow,
            org=org,
            user=other_user,
        )

        client.force_login(guest)
        response = client.get(reverse("validations:guest_validation_list"))

        assert response.status_code == HTTPStatus.OK
        validations = list(response.context["validations"])
        assert guest_run in validations
        assert other_run not in validations

    def test_guest_sees_runs_from_multiple_workflows(self, client):
        """Test that guest sees runs from all accessible workflows."""
        from validibot.submissions.tests.factories import SubmissionFactory
        from validibot.validations.tests.factories import ValidationRunFactory

        org1 = OrganizationFactory(name="Org One")
        org2 = OrganizationFactory(name="Org Two")
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()

        workflow1 = WorkflowFactory(org=org1, is_active=True, name="Workflow One")
        workflow2 = WorkflowFactory(org=org2, is_active=True, name="Workflow Two")

        WorkflowAccessGrant.objects.create(
            workflow=workflow1,
            user=guest,
            is_active=True,
        )
        WorkflowAccessGrant.objects.create(
            workflow=workflow2,
            user=guest,
            is_active=True,
        )

        sub1 = SubmissionFactory(workflow=workflow1, org=org1, user=guest)
        sub2 = SubmissionFactory(workflow=workflow2, org=org2, user=guest)

        run1 = ValidationRunFactory(
            submission=sub1,
            workflow=workflow1,
            org=org1,
            user=guest,
        )
        run2 = ValidationRunFactory(
            submission=sub2,
            workflow=workflow2,
            org=org2,
            user=guest,
        )

        client.force_login(guest)
        response = client.get(reverse("validations:guest_validation_list"))

        assert response.status_code == HTTPStatus.OK
        validations = list(response.context["validations"])
        assert run1 in validations
        assert run2 in validations

    def test_org_filter(self, client):
        """Test that org filter works for guest validation list."""
        from validibot.submissions.tests.factories import SubmissionFactory
        from validibot.validations.tests.factories import ValidationRunFactory

        org1 = OrganizationFactory(name="Org One")
        org2 = OrganizationFactory(name="Org Two")
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()

        workflow1 = WorkflowFactory(org=org1, is_active=True)
        workflow2 = WorkflowFactory(org=org2, is_active=True)

        WorkflowAccessGrant.objects.create(
            workflow=workflow1,
            user=guest,
            is_active=True,
        )
        WorkflowAccessGrant.objects.create(
            workflow=workflow2,
            user=guest,
            is_active=True,
        )

        sub1 = SubmissionFactory(workflow=workflow1, org=org1, user=guest)
        sub2 = SubmissionFactory(workflow=workflow2, org=org2, user=guest)

        run1 = ValidationRunFactory(
            submission=sub1,
            workflow=workflow1,
            org=org1,
            user=guest,
        )
        run2 = ValidationRunFactory(
            submission=sub2,
            workflow=workflow2,
            org=org2,
            user=guest,
        )

        client.force_login(guest)

        # Filter by org1
        response = client.get(
            reverse("validations:guest_validation_list") + f"?org={org1.pk}",
        )

        assert response.status_code == HTTPStatus.OK
        validations = list(response.context["validations"])
        assert run1 in validations
        assert run2 not in validations

    def test_status_filter(self, client):
        """Test that status filter works for guest validation list."""
        from validibot.submissions.tests.factories import SubmissionFactory
        from validibot.validations.constants import ValidationRunStatus
        from validibot.validations.tests.factories import ValidationRunFactory

        org = OrganizationFactory()
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()

        workflow = WorkflowFactory(org=org, is_active=True)
        WorkflowAccessGrant.objects.create(
            workflow=workflow,
            user=guest,
            is_active=True,
        )

        sub1 = SubmissionFactory(workflow=workflow, org=org, user=guest)
        sub2 = SubmissionFactory(workflow=workflow, org=org, user=guest)

        run_pending = ValidationRunFactory(
            submission=sub1,
            workflow=workflow,
            org=org,
            user=guest,
            status=ValidationRunStatus.PENDING,
        )
        run_succeeded = ValidationRunFactory(
            submission=sub2,
            workflow=workflow,
            org=org,
            user=guest,
            status=ValidationRunStatus.SUCCEEDED,
        )

        client.force_login(guest)

        # Filter by SUCCEEDED status
        response = client.get(
            reverse("validations:guest_validation_list")
            + f"?status={ValidationRunStatus.SUCCEEDED}",
        )

        assert response.status_code == HTTPStatus.OK
        validations = list(response.context["validations"])
        assert run_succeeded in validations
        assert run_pending not in validations

    def test_requires_login(self, client):
        """Test that view requires login."""
        response = client.get(reverse("validations:guest_validation_list"))

        assert response.status_code == HTTPStatus.FOUND
        assert "/accounts/login/" in response.url


# =============================================================================
# GuestAwareThrottle Tests
# =============================================================================


class TestGuestAwareThrottle:
    """Tests for the GuestAwareThrottle class."""

    def test_guest_uses_guest_scope(self):
        """Test that guest users get guest-specific throttle scope."""
        from unittest.mock import MagicMock

        from validibot.core.throttles import GuestAwareThrottle

        throttle = GuestAwareThrottle()
        throttle.scope = "workflow_launch"

        # Mock the THROTTLE_RATES class attribute
        throttle.THROTTLE_RATES = {
            "workflow_launch": "60/minute",
            "guest_workflow_launch": "20/minute",
        }

        # Create mock request with guest user
        request = MagicMock()
        request.user.is_authenticated = True
        request.user.is_workflow_guest = True

        # Create mock view
        view = MagicMock()

        # Call get_cache_key to trigger scope modification
        throttle.get_cache_key(request, view)

        # Scope should be updated to guest scope
        assert throttle.scope == "guest_workflow_launch"

    def test_member_uses_normal_scope(self):
        """Test that org members use normal throttle scope."""
        from unittest.mock import MagicMock

        from validibot.core.throttles import GuestAwareThrottle

        throttle = GuestAwareThrottle()
        throttle.scope = "workflow_launch"

        throttle.THROTTLE_RATES = {
            "workflow_launch": "60/minute",
            "guest_workflow_launch": "20/minute",
        }

        # Create mock request with non-guest user
        request = MagicMock()
        request.user.is_authenticated = True
        request.user.is_workflow_guest = False

        view = MagicMock()

        throttle.get_cache_key(request, view)

        # Scope should remain unchanged
        assert throttle.scope == "workflow_launch"

    def test_anonymous_returns_none(self):
        """Test that anonymous users return None for cache key."""
        from unittest.mock import MagicMock

        from validibot.core.throttles import GuestAwareThrottle

        throttle = GuestAwareThrottle()
        throttle.scope = "workflow_launch"

        # Create mock request with anonymous user
        request = MagicMock()
        request.user.is_authenticated = False

        view = MagicMock()

        result = throttle.get_cache_key(request, view)

        # Should return None for anonymous users
        assert result is None

    def test_guest_fallback_when_no_guest_rate(self):
        """Test that guest falls back to normal scope if no guest rate defined."""
        from unittest.mock import MagicMock

        from validibot.core.throttles import GuestAwareThrottle

        throttle = GuestAwareThrottle()
        throttle.scope = "workflow_launch"

        # Only normal rate defined, no guest rate
        throttle.THROTTLE_RATES = {
            "workflow_launch": "60/minute",
        }

        request = MagicMock()
        request.user.is_authenticated = True
        request.user.is_workflow_guest = True

        view = MagicMock()

        throttle.get_cache_key(request, view)

        # Scope should remain unchanged (no guest_ version available)
        assert throttle.scope == "workflow_launch"


# =============================================================================
# GuestInvite Model Tests
# =============================================================================


class TestGuestInvite:
    """Tests for the GuestInvite model (org-level guest invites)."""

    def test_create_guest_invite_with_selected_scope(self):
        """Test creating a guest invite with selected workflows."""
        org = OrganizationFactory()
        inviter = UserFactory()
        inviter.memberships.create(org=org, is_active=True)
        invitee = UserFactory(orgs=[])

        workflow1 = WorkflowFactory(org=org, is_active=True)
        workflow2 = WorkflowFactory(org=org, is_active=True)

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email=invitee.email,
            invitee_user=invitee,
            scope=GuestInvite.Scope.SELECTED,
            workflows=[workflow1, workflow2],
            send_email=False,
        )

        assert invite.org == org
        assert invite.inviter == inviter
        assert invite.invitee_user == invitee
        assert invite.scope == GuestInvite.Scope.SELECTED
        assert invite.status == GuestInvite.Status.PENDING
        assert set(invite.workflows.all()) == {workflow1, workflow2}

    def test_create_guest_invite_with_all_scope(self):
        """Test creating a guest invite with ALL workflows scope."""
        org = OrganizationFactory()
        inviter = UserFactory()
        inviter.memberships.create(org=org, is_active=True)

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email="guest@example.com",
            scope=GuestInvite.Scope.ALL,
            send_email=False,
        )

        assert invite.scope == GuestInvite.Scope.ALL
        assert invite.workflows.count() == 0  # Workflows not stored for ALL scope

    def test_get_resolved_workflows_selected_scope(self):
        """Test get_resolved_workflows returns selected workflows."""
        org = OrganizationFactory()
        inviter = UserFactory()
        inviter.memberships.create(org=org, is_active=True)

        workflow1 = WorkflowFactory(org=org, is_active=True)
        workflow2 = WorkflowFactory(org=org, is_active=True)
        workflow3 = WorkflowFactory(org=org, is_active=True)  # Not selected

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email="guest@example.com",
            scope=GuestInvite.Scope.SELECTED,
            workflows=[workflow1, workflow2],
            send_email=False,
        )

        resolved = list(invite.get_resolved_workflows())
        assert workflow1 in resolved
        assert workflow2 in resolved
        assert workflow3 not in resolved

    def test_get_resolved_workflows_all_scope(self):
        """Test get_resolved_workflows returns all active workflows for ALL scope."""
        org = OrganizationFactory()
        inviter = UserFactory()
        inviter.memberships.create(org=org, is_active=True)

        workflow1 = WorkflowFactory(org=org, is_active=True, is_archived=False)
        workflow2 = WorkflowFactory(org=org, is_active=True, is_archived=False)
        inactive_workflow = WorkflowFactory(org=org, is_active=False)
        archived_workflow = WorkflowFactory(org=org, is_active=True, is_archived=True)

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email="guest@example.com",
            scope=GuestInvite.Scope.ALL,
            send_email=False,
        )

        resolved = list(invite.get_resolved_workflows())
        assert workflow1 in resolved
        assert workflow2 in resolved
        assert inactive_workflow not in resolved
        assert archived_workflow not in resolved

    def test_accept_guest_invite_creates_grants(self):
        """Test accepting guest invite creates WorkflowAccessGrants."""
        org = OrganizationFactory()
        inviter = UserFactory()
        inviter.memberships.create(org=org, is_active=True)
        invitee = UserFactory(orgs=[])
        Membership.objects.filter(user=invitee).delete()

        workflow1 = WorkflowFactory(org=org, is_active=True)
        workflow2 = WorkflowFactory(org=org, is_active=True)

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email=invitee.email,
            invitee_user=invitee,
            scope=GuestInvite.Scope.SELECTED,
            workflows=[workflow1, workflow2],
            send_email=False,
        )

        grants = invite.accept()

        assert len(grants) == 2  # noqa: PLR2004
        assert invite.status == GuestInvite.Status.ACCEPTED
        assert WorkflowAccessGrant.objects.filter(
            user=invitee,
            workflow=workflow1,
            is_active=True,
        ).exists()
        assert WorkflowAccessGrant.objects.filter(
            user=invitee,
            workflow=workflow2,
            is_active=True,
        ).exists()

    def test_accept_guest_invite_with_all_scope(self):
        """Test accepting ALL scope invite creates grants for all workflows."""
        org = OrganizationFactory()
        inviter = UserFactory()
        inviter.memberships.create(org=org, is_active=True)
        invitee = UserFactory(orgs=[])
        Membership.objects.filter(user=invitee).delete()

        WorkflowFactory(org=org, is_active=True)
        WorkflowFactory(org=org, is_active=True)
        WorkflowFactory(org=org, is_active=False)  # inactive, should be skipped

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email=invitee.email,
            invitee_user=invitee,
            scope=GuestInvite.Scope.ALL,
            send_email=False,
        )

        grants = invite.accept()

        assert len(grants) == 2  # noqa: PLR2004  # Only active workflows
        assert invitee.is_workflow_guest is True

    def test_decline_guest_invite(self):
        """Test declining a guest invite."""
        org = OrganizationFactory()
        inviter = UserFactory()
        inviter.memberships.create(org=org, is_active=True)

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email="guest@example.com",
            scope=GuestInvite.Scope.ALL,
            send_email=False,
        )

        invite.decline()

        assert invite.status == GuestInvite.Status.DECLINED

    def test_cancel_guest_invite(self):
        """Test canceling a guest invite."""
        org = OrganizationFactory()
        inviter = UserFactory()
        inviter.memberships.create(org=org, is_active=True)

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email="guest@example.com",
            scope=GuestInvite.Scope.ALL,
            send_email=False,
        )

        invite.cancel()

        assert invite.status == GuestInvite.Status.CANCELED

    def test_cannot_accept_non_pending_invite(self):
        """Test that accepting a non-pending invite raises ValueError."""
        org = OrganizationFactory()
        inviter = UserFactory()
        inviter.memberships.create(org=org, is_active=True)
        invitee = UserFactory(orgs=[])

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email=invitee.email,
            invitee_user=invitee,
            scope=GuestInvite.Scope.ALL,
            send_email=False,
        )
        invite.decline()

        with pytest.raises(ValueError, match="Cannot accept invite"):
            invite.accept()

    def test_accept_expired_invite_raises_error(self):
        """Test that accepting an expired invite raises ValueError."""
        org = OrganizationFactory()
        inviter = UserFactory()
        inviter.memberships.create(org=org, is_active=True)
        invitee = UserFactory(orgs=[])

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email=invitee.email,
            invitee_user=invitee,
            scope=GuestInvite.Scope.ALL,
            expiry_days=-1,  # Already expired
            send_email=False,
        )

        with pytest.raises(ValueError, match="expired"):
            invite.accept()

    def test_is_pending_property(self):
        """Test the is_pending property."""
        org = OrganizationFactory()
        inviter = UserFactory()
        inviter.memberships.create(org=org, is_active=True)

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email="guest@example.com",
            scope=GuestInvite.Scope.ALL,
            send_email=False,
        )

        assert invite.is_pending is True

        invite.decline()
        assert invite.is_pending is False


# =============================================================================
# Sharing Permission Tests
# =============================================================================


class TestWorkflowSharingPermissions:
    """Tests for workflow sharing permission logic."""

    def test_admin_can_manage_sharing_for_any_workflow(self, client):
        """Test that org admins can manage sharing for any workflow."""
        org = OrganizationFactory()
        admin = UserFactory()
        grant_role(admin, org, RoleCode.ADMIN)

        # Workflow created by another user
        author = UserFactory()
        grant_role(author, org, RoleCode.AUTHOR)
        workflow = WorkflowFactory(org=org, user=author)

        client.force_login(admin)
        session = client.session
        session["active_org_id"] = str(org.id)
        session.save()

        # Admin should be able to access sharing page
        response = client.get(
            reverse("workflows:workflow_sharing", kwargs={"pk": workflow.pk}),
        )
        assert response.status_code == HTTPStatus.OK

    def test_author_can_manage_sharing_for_own_workflow(self, client):
        """Test that authors can manage sharing for workflows they created."""
        org = OrganizationFactory()
        author = UserFactory()
        grant_role(author, org, RoleCode.AUTHOR)

        workflow = WorkflowFactory(org=org, user=author)

        client.force_login(author)
        session = client.session
        session["active_org_id"] = str(org.id)
        session.save()

        response = client.get(
            reverse("workflows:workflow_sharing", kwargs={"pk": workflow.pk}),
        )
        assert response.status_code == HTTPStatus.OK

    def test_author_cannot_manage_sharing_for_others_workflow(self, client):
        """Test that authors cannot manage sharing for workflows they didn't create."""
        org = OrganizationFactory()

        # Create two authors
        author1 = UserFactory()
        grant_role(author1, org, RoleCode.AUTHOR)

        author2 = UserFactory()
        grant_role(author2, org, RoleCode.AUTHOR)

        # Workflow created by author1
        workflow = WorkflowFactory(org=org, user=author1)

        # Author2 tries to access sharing
        client.force_login(author2)
        session = client.session
        session["active_org_id"] = str(org.id)
        session.save()

        response = client.get(
            reverse("workflows:workflow_sharing", kwargs={"pk": workflow.pk}),
        )
        assert response.status_code == HTTPStatus.FORBIDDEN

    def test_viewer_cannot_manage_sharing(self, client):
        """Test that viewers cannot manage sharing for any workflow."""
        org = OrganizationFactory()

        author = UserFactory()
        grant_role(author, org, RoleCode.AUTHOR)

        viewer = UserFactory()
        grant_role(viewer, org, RoleCode.WORKFLOW_VIEWER)

        workflow = WorkflowFactory(org=org, user=author)

        client.force_login(viewer)
        session = client.session
        session["active_org_id"] = str(org.id)
        session.save()

        response = client.get(
            reverse("workflows:workflow_sharing", kwargs={"pk": workflow.pk}),
        )
        assert response.status_code == HTTPStatus.FORBIDDEN
