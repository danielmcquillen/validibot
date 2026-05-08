"""Tests for the ``OrgGuestAccess`` model and the GuestInvite ALL-scope flow.

This module covers three concerns:

1. **Model basics** — uniqueness on (user, org), revoke flips
   ``is_active`` rather than deleting, str repr is human-readable.
2. **Acceptance flow** — ``GuestInvite`` with ``scope=ALL`` creates
   exactly one ``OrgGuestAccess`` row instead of expanding into N
   per-workflow grants. Re-acceptance after revocation reactivates
   the same row (preserving the original grant timestamp).
3. **Independence from per-workflow grants** — a user can hold an
   ``OrgGuestAccess`` for one org AND ``WorkflowAccessGrant`` rows
   for individual workflows in another org without conflict.

The read-side (queryset narrowing) lives in
:mod:`~validibot.workflows.models.WorkflowQuerySet.for_user` and is
covered by separate tests once Phase 5 lands.
"""

from __future__ import annotations

import pytest
from django.db import IntegrityError

from validibot.users.models import Membership
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.workflows.models import GuestInvite
from validibot.workflows.models import OrgGuestAccess
from validibot.workflows.models import WorkflowAccessGrant
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


# =============================================================================
# Model basics
# =============================================================================


class TestOrgGuestAccessModel:
    """Field constraints, revocation behaviour, and string repr."""

    def test_unique_per_user_org(self):
        """Cannot create two active rows for the same (user, org) pair.

        The unique constraint enforces that an org grants a guest
        either holds or doesn't hold org-wide access — there's no
        meaningful "two grants" state. ``get_or_create`` in the
        acceptance flow relies on this uniqueness invariant.
        """

        org = OrganizationFactory()
        user = UserFactory(orgs=[])
        OrgGuestAccess.objects.create(user=user, org=org, is_active=True)

        with pytest.raises(IntegrityError):
            OrgGuestAccess.objects.create(user=user, org=org, is_active=True)

    def test_revoke_flips_is_active(self):
        """``revoke()`` sets is_active=False without deleting the row.

        Keeping the row preserves the audit trail (granted_by,
        created timestamp) so an operator investigating later can see
        when access was granted and when it was revoked. A
        re-acceptance flow can also reactivate the same row, keeping
        the original grant timestamp intact.
        """

        org = OrganizationFactory()
        user = UserFactory(orgs=[])
        access = OrgGuestAccess.objects.create(
            user=user,
            org=org,
            is_active=True,
        )

        access.revoke()

        access.refresh_from_db()
        assert access.is_active is False
        # The row still exists.
        assert OrgGuestAccess.objects.filter(pk=access.pk).exists()

    def test_str_repr_is_human_readable(self):
        """``__str__`` should describe the relationship clearly."""

        org = OrganizationFactory(name="Acme Corp")
        user = UserFactory(orgs=[], username="visitor")
        access = OrgGuestAccess.objects.create(
            user=user,
            org=org,
            is_active=True,
        )
        repr_str = str(access)
        assert "visitor" in repr_str
        assert "Acme Corp" in repr_str


# =============================================================================
# Acceptance flow integration
# =============================================================================


class TestGuestInviteAllScopeCreatesOrgGuestAccess:
    """The accept() rewrite: ALL scope → OrgGuestAccess, not bulk grants."""

    def test_first_acceptance_creates_active_row(self):
        """A fresh ALL-scope acceptance creates an active OrgGuestAccess."""

        org = OrganizationFactory()
        inviter = UserFactory()
        inviter.memberships.create(org=org, is_active=True)
        invitee = UserFactory(orgs=[])
        Membership.objects.filter(user=invitee).delete()
        WorkflowFactory(org=org)

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email=invitee.email,
            invitee_user=invitee,
            scope=GuestInvite.Scope.ALL,
            send_email=False,
        )
        result = invite.accept()

        assert isinstance(result, OrgGuestAccess)
        assert result.is_active is True
        assert result.granted_by == inviter

    def test_reacceptance_after_revoke_reactivates_same_row(self):
        """Re-inviting a previously-revoked guest reuses the existing row.

        Preserves the original ``created`` timestamp + audit trail so
        an operator can see "this access was first granted on X,
        revoked on Y, restored on Z" rather than two unrelated rows.
        """

        org = OrganizationFactory()
        inviter = UserFactory()
        inviter.memberships.create(org=org, is_active=True)
        invitee = UserFactory(orgs=[])
        Membership.objects.filter(user=invitee).delete()
        WorkflowFactory(org=org)

        # First invite + accept + revoke.
        invite1 = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email=invitee.email,
            invitee_user=invitee,
            scope=GuestInvite.Scope.ALL,
            send_email=False,
        )
        access1 = invite1.accept()
        original_pk = access1.pk
        access1.revoke()

        # Second invite + accept.
        invite2 = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email=invitee.email,
            invitee_user=invitee,
            scope=GuestInvite.Scope.ALL,
            send_email=False,
        )
        access2 = invite2.accept()

        # Same row reactivated, not a new one.
        assert access2.pk == original_pk
        assert access2.is_active is True

    def test_all_scope_does_not_create_workflow_grants(self):
        """Confirms the model break from the legacy expansion behaviour.

        Under the old model, ALL scope created N WorkflowAccessGrant
        rows. The new model creates ZERO grants; the read-side
        queryset consults OrgGuestAccess directly. This test pins the
        absence of grants so a future regression that re-introduces
        the expansion would fail loudly.
        """

        org = OrganizationFactory()
        inviter = UserFactory()
        inviter.memberships.create(org=org, is_active=True)
        invitee = UserFactory(orgs=[])
        Membership.objects.filter(user=invitee).delete()
        WorkflowFactory(org=org)
        WorkflowFactory(org=org)

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email=invitee.email,
            invitee_user=invitee,
            scope=GuestInvite.Scope.ALL,
            send_email=False,
        )
        invite.accept()

        assert not WorkflowAccessGrant.objects.filter(
            user=invitee,
            workflow__org=org,
        ).exists()

    def test_selected_scope_still_creates_per_workflow_grants(self):
        """SELECTED scope is unchanged — still creates one grant per workflow.

        Pin: the OrgGuestAccess change applies ONLY to ALL-scope
        acceptance. SELECTED scope retains its per-workflow expansion
        because the operator's intent was a fixed subset, not org-wide
        access to the catalog.
        """

        org = OrganizationFactory()
        inviter = UserFactory()
        inviter.memberships.create(org=org, is_active=True)
        invitee = UserFactory(orgs=[])
        Membership.objects.filter(user=invitee).delete()
        wf1 = WorkflowFactory(org=org)
        wf2 = WorkflowFactory(org=org)

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email=invitee.email,
            invitee_user=invitee,
            scope=GuestInvite.Scope.SELECTED,
            workflows=[wf1, wf2],
            send_email=False,
        )
        result = invite.accept()

        assert isinstance(result, list)
        assert len(result) == 2  # noqa: PLR2004
        assert all(isinstance(grant, WorkflowAccessGrant) for grant in result)
        # AND no OrgGuestAccess row was created — SELECTED is per-workflow only.
        assert not OrgGuestAccess.objects.filter(user=invitee, org=org).exists()


# =============================================================================
# WorkflowQuerySet.for_user — read-side coverage for new access paths
# =============================================================================


class TestForUserOrgGuestAccessBranch:
    """``for_user`` honours active OrgGuestAccess rows.

    Pin the read-side payoff of the new model: a guest with org-wide
    access sees every workflow in the org, including ones added after
    the grant was issued. This is the whole reason for OrgGuestAccess
    over per-workflow grants.
    """

    def test_org_guest_access_makes_org_workflows_visible(self):
        """One OrgGuestAccess row → every workflow in the org is visible."""
        from validibot.workflows.models import Workflow

        org = OrganizationFactory()
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()
        OrgGuestAccess.objects.create(user=guest, org=org, is_active=True)

        wf1 = WorkflowFactory(org=org)
        wf2 = WorkflowFactory(org=org)

        visible = set(Workflow.objects.for_user(guest).values_list("pk", flat=True))
        assert wf1.pk in visible
        assert wf2.pk in visible

    def test_future_workflows_in_org_become_visible_automatically(self):
        """A workflow created AFTER the grant is also visible.

        This is the property OrgGuestAccess provides that bulk-grant
        expansion can't: the read-side queryset consults the row at
        query time, so the org's catalog growth is automatically
        reflected without admin intervention.
        """
        from validibot.workflows.models import Workflow

        org = OrganizationFactory()
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()
        OrgGuestAccess.objects.create(user=guest, org=org, is_active=True)

        # Initially no workflows; later one is added.
        WorkflowFactory(org=org)
        late_arrival = WorkflowFactory(org=org)

        visible = set(Workflow.objects.for_user(guest).values_list("pk", flat=True))
        assert late_arrival.pk in visible

    def test_inactive_org_guest_access_does_not_grant_visibility(self):
        """An ``is_active=False`` row does NOT make workflows visible.

        Revocation is a flag flip; the row remains for audit. The
        read-side filters on ``is_active=True`` so a revoked row stops
        granting access immediately.
        """
        from validibot.workflows.models import Workflow

        org = OrganizationFactory()
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()
        OrgGuestAccess.objects.create(user=guest, org=org, is_active=False)

        wf = WorkflowFactory(org=org)

        visible = set(Workflow.objects.for_user(guest).values_list("pk", flat=True))
        assert wf.pk not in visible

    def test_org_guest_access_in_one_org_does_not_leak_to_other_orgs(self):
        """An OrgGuestAccess for Org A does NOT grant access to Org B.

        Multi-tenant isolation pin: this is the test that catches a
        regression where the queryset filters lose their per-org
        subquery binding.
        """
        from validibot.workflows.models import Workflow

        org_a = OrganizationFactory()
        org_b = OrganizationFactory()
        guest = UserFactory(orgs=[])
        Membership.objects.filter(user=guest).delete()
        OrgGuestAccess.objects.create(user=guest, org=org_a, is_active=True)

        wf_a = WorkflowFactory(org=org_a)
        wf_b = WorkflowFactory(org=org_b)

        visible = set(Workflow.objects.for_user(guest).values_list("pk", flat=True))
        assert wf_a.pk in visible
        assert wf_b.pk not in visible


class TestForUserPublicBranch:
    """``for_user`` includes ``is_public=True`` workflows for any auth user.

    Public workflows are the author's deliberate choice — the
    visibility is platform-wide, not per-org. Anyone authenticated
    can see them.
    """

    def test_public_workflow_visible_to_unrelated_user(self):
        """A user with no membership/grant sees a public workflow."""
        from validibot.workflows.models import Workflow

        author = UserFactory()
        public_wf = WorkflowFactory(user=author, is_public=True)

        unrelated = UserFactory(orgs=[])
        Membership.objects.filter(user=unrelated).delete()

        visible = set(
            Workflow.objects.for_user(unrelated).values_list("pk", flat=True),
        )
        assert public_wf.pk in visible

    def test_non_public_workflow_invisible_without_other_path(self):
        """``is_public=False`` blocks visibility absent membership/grant.

        Pin to ensure the public branch is the *only* thing making a
        public workflow visible — flipping it off should hide the row
        for users without other access paths.
        """
        from validibot.workflows.models import Workflow

        author = UserFactory()
        private_wf = WorkflowFactory(user=author, is_public=False)

        unrelated = UserFactory(orgs=[])
        Membership.objects.filter(user=unrelated).delete()

        visible = set(
            Workflow.objects.for_user(unrelated).values_list("pk", flat=True),
        )
        assert private_wf.pk not in visible
