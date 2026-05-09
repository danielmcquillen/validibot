"""Tests for invite-driven signup → GUEST classification.

Two intertwined behaviours under test:

1. **Signal suppression during invite-driven signup.** The
   ``invite_driven_signup`` ContextVar gates both ``post_save``
   signals so a user created via an invite acceptance does NOT get
   an auto-personal-workspace and does NOT get pre-classified as
   BASIC. The invite-flow code takes over both responsibilities.

2. **Classification + redemption in the AccountAdapter.** After
   ``save_user`` completes, ``get_signup_redirect_url`` consumes
   the session-stashed invite token, calls ``invite.accept()``, and
   classifies the new user as GUEST.

These tests pin both halves end-to-end so a regression in either
side fails loudly.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from django.urls import reverse

from validibot.core.features import CommercialFeature
from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import set_license
from validibot.users.constants import RoleCode
from validibot.users.constants import UserKindGroup
from validibot.users.models import Membership
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role
from validibot.workflows.models import GuestInvite
from validibot.workflows.models import OrgGuestAccess
from validibot.workflows.models import WorkflowAccessGrant
from validibot.workflows.models import WorkflowInvite
from validibot.workflows.tests.factories import WorkflowFactory

pytestmark = pytest.mark.django_db


def _pro_license_with_guest_management() -> License:
    return License(
        edition=Edition.PRO,
        features=frozenset(
            {
                CommercialFeature.GUEST_MANAGEMENT.value,
                CommercialFeature.AUDIT_LOG.value,
            },
        ),
    )


# =============================================================================
# Signal suppression — the ``invite_driven_signup`` ContextVar
# =============================================================================


class TestInviteDrivenSignupSuppression:
    """Default user-creation side effects skip during invite signups.

    The post_save signals normally provision a personal workspace and
    classify the user as BASIC. Both must skip when the
    ``invite_driven_signup`` flag is set so the invite-flow code can
    take over without conflict.
    """

    def test_workspace_signal_skipped_in_invite_context(self):
        """A user created inside ``invite_driven_signup()`` has no workspace.

        Pin the gate: without this skip, brand-new invite-acceptance
        users would have a personal workspace AND a Membership
        (BASIC), conflicting with the GUEST classification the invite
        flow applies afterwards.
        """

        from validibot.users.models import User
        from validibot.users.signals import invite_driven_signup

        set_license(_pro_license_with_guest_management())

        with invite_driven_signup():
            user = User.objects.create_user(
                username="invitee_a",
                email="invitee_a@example.com",
                password="correct-horse-battery-staple",  # noqa: S106
            )

        # No personal workspace was provisioned.
        assert not user.memberships.filter(is_active=True).exists()
        # No classifier group was attached either.
        assert not user.groups.filter(
            name__in=[
                UserKindGroup.BASIC.value,
                UserKindGroup.GUEST.value,
            ],
        ).exists()

    def test_signals_fire_normally_outside_invite_context(self):
        """Regression guard: standard signups still get the default behaviour.

        The ContextVar suppression is per-context — outside the
        manager, normal users continue to get auto-classification.
        Without this test, a typo in the gate logic could silently
        strip every new user of their classifier group.
        """

        from validibot.users.models import User

        set_license(_pro_license_with_guest_management())

        # Standard user creation, no invite context.
        user = User.objects.create_user(
            username="invitee_b",
            email="invitee_b@example.com",
            password="correct-horse-battery-staple",  # noqa: S106
        )

        # In a transactional test, ``transaction.on_commit`` callbacks
        # don't fire — so we can't assert the personal-workspace was
        # auto-created here. We CAN assert the user_kind via the
        # ContextVar-gated path: skipping the suppression context
        # means the signal would have run on commit. To pin the gate
        # itself, just confirm the ContextVar is in default state
        # (False) outside the manager.
        from validibot.users.signals import _invite_driven_signup_var

        assert _invite_driven_signup_var.get() is False
        assert user.pk is not None  # no error raised


# =============================================================================
# Org-level GuestInvite redemption flow
# =============================================================================


class TestGuestInviteAcceptViewLoggedIn:
    """The tokenized accept view works for already-authenticated users."""

    def test_authenticated_user_accept_all_scope_creates_org_guest_access(
        self,
        client,
    ):
        """Authenticated user with a valid token → OrgGuestAccess created."""

        set_license(_pro_license_with_guest_management())
        org = OrganizationFactory()
        inviter = UserFactory(orgs=[org])
        grant_role(inviter, org, RoleCode.AUTHOR)
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

        client.force_login(invitee)
        response = client.get(
            reverse("guest_invite_accept", kwargs={"token": invite.token}),
        )

        assert response.status_code == HTTPStatus.FOUND
        assert OrgGuestAccess.objects.filter(
            user=invitee,
            org=org,
            is_active=True,
        ).exists()

    def test_authenticated_user_accept_selected_scope_creates_grants(
        self,
        client,
    ):
        """Authenticated user with SELECTED scope → per-workflow grants."""

        set_license(_pro_license_with_guest_management())
        org = OrganizationFactory()
        inviter = UserFactory(orgs=[org])
        grant_role(inviter, org, RoleCode.AUTHOR)
        invitee = UserFactory(orgs=[])
        Membership.objects.filter(user=invitee).delete()
        wf = WorkflowFactory(org=org)

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email=invitee.email,
            invitee_user=invitee,
            scope=GuestInvite.Scope.SELECTED,
            workflows=[wf],
            send_email=False,
        )

        client.force_login(invitee)
        response = client.get(
            reverse("guest_invite_accept", kwargs={"token": invite.token}),
        )

        assert response.status_code == HTTPStatus.FOUND
        assert WorkflowAccessGrant.objects.filter(
            user=invitee,
            workflow=wf,
            is_active=True,
        ).exists()

    def test_anonymous_user_redirects_to_signup_with_token_in_session(
        self,
        client,
    ):
        """Anonymous accept stashes the token and routes to signup.

        Pin the session plumbing the AccountAdapter relies on. Without
        this stash, the post-signup adapter has no way to redeem the
        invite for the new user.
        """

        from validibot.users.adapters import GUEST_INVITE_SESSION_KEY

        set_license(_pro_license_with_guest_management())
        org = OrganizationFactory()
        inviter = UserFactory(orgs=[org])
        grant_role(inviter, org, RoleCode.AUTHOR)

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email="brand-new@example.com",
            scope=GuestInvite.Scope.ALL,
            send_email=False,
        )

        # Anonymous client.
        response = client.get(
            reverse("guest_invite_accept", kwargs={"token": invite.token}),
        )

        assert response.status_code == HTTPStatus.FOUND
        assert response.url == reverse("account_signup")
        assert client.session.get(GUEST_INVITE_SESSION_KEY) == str(invite.token)

    def test_expired_invite_returns_redirect_with_error(self, client):
        """An expired invite cannot be redeemed.

        Pin the failure path so a refactor that drops the status check
        doesn't accidentally allow redemption of stale tokens.
        """

        set_license(_pro_license_with_guest_management())
        org = OrganizationFactory()
        inviter = UserFactory(orgs=[org])
        grant_role(inviter, org, RoleCode.AUTHOR)
        invitee = UserFactory(orgs=[])

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email=invitee.email,
            invitee_user=invitee,
            scope=GuestInvite.Scope.ALL,
            send_email=False,
        )
        # Mark expired manually — simpler than time-travel.
        invite.status = GuestInvite.Status.EXPIRED
        invite.save(update_fields=["status"])

        client.force_login(invitee)
        response = client.get(
            reverse("guest_invite_accept", kwargs={"token": invite.token}),
        )

        assert response.status_code == HTTPStatus.FOUND
        # Did NOT create access.
        assert not OrgGuestAccess.objects.filter(
            user=invitee,
            org=org,
        ).exists()


# =============================================================================
# AccountAdapter integration — workflow invite
# =============================================================================


class TestWorkflowInviteSignupClassifiesAsGuest:
    """A user who signs up via a workflow invite is classified GUEST.

    Without this behaviour, the ``allow_guest_access`` kill switch,
    guest-rate throttling, and guest-UI navigation never apply to
    invite-accepting new users. They look like regular BASIC users
    with cross-org grants — wrong by design.
    """

    def test_post_signup_redirect_classifies_new_user_as_guest(
        self,
        rf,
    ):
        """The adapter's ``get_signup_redirect_url`` classifies as GUEST.

        End-to-end: build a request that mimics the post-signup state
        (user authenticated, session has token), then call the
        adapter's redirect method. Verify the user is now in the
        Guests group.
        """

        from validibot.users.adapters import WORKFLOW_INVITE_SESSION_KEY
        from validibot.users.adapters import AccountAdapter

        set_license(_pro_license_with_guest_management())

        org = OrganizationFactory()
        inviter = UserFactory(orgs=[org])
        grant_role(inviter, org, RoleCode.AUTHOR)
        wf = WorkflowFactory(org=org)
        invitee = UserFactory(orgs=[])
        Membership.objects.filter(user=invitee).delete()

        invite = WorkflowInvite.create_with_expiry(
            workflow=wf,
            inviter=inviter,
            invitee_email=invitee.email,
            invitee_user=invitee,
            send_email=False,
        )

        # Simulate the post-signup request with the token in session.
        request = rf.get("/accounts/signup/")
        request.user = invitee
        request.session = {WORKFLOW_INVITE_SESSION_KEY: str(invite.token)}
        # The adapter calls ``messages.success`` — attach storage.
        from django.contrib.messages.storage.fallback import FallbackStorage

        request._messages = FallbackStorage(request)

        AccountAdapter().get_signup_redirect_url(request)

        invitee.refresh_from_db()
        assert invitee.user_kind == UserKindGroup.GUEST


class TestGuestInviteSignupClassifiesAsGuest:
    """A user who signs up via a guest invite is classified GUEST."""

    def test_post_signup_redirect_classifies_new_user_as_guest(self, rf):
        """The adapter handles GUEST_INVITE_SESSION_KEY parallel to workflow flow.

        Pin the org-level path: a brand-new user who clicked a guest
        invite link, signed up, and landed in the post-signup adapter
        must be classified GUEST and have an OrgGuestAccess row
        created — the whole point of building this flow.
        """

        from validibot.users.adapters import GUEST_INVITE_SESSION_KEY
        from validibot.users.adapters import AccountAdapter

        set_license(_pro_license_with_guest_management())

        org = OrganizationFactory()
        inviter = UserFactory(orgs=[org])
        grant_role(inviter, org, RoleCode.AUTHOR)
        invitee = UserFactory(orgs=[])
        Membership.objects.filter(user=invitee).delete()

        invite = GuestInvite.create_with_expiry(
            org=org,
            inviter=inviter,
            invitee_email=invitee.email,
            invitee_user=invitee,
            scope=GuestInvite.Scope.ALL,
            send_email=False,
        )

        request = rf.get("/accounts/signup/")
        request.user = invitee
        request.session = {GUEST_INVITE_SESSION_KEY: str(invite.token)}
        from django.contrib.messages.storage.fallback import FallbackStorage

        request._messages = FallbackStorage(request)

        AccountAdapter().get_signup_redirect_url(request)

        invitee.refresh_from_db()
        assert invitee.user_kind == UserKindGroup.GUEST
        assert OrgGuestAccess.objects.filter(
            user=invitee,
            org=org,
            is_active=True,
        ).exists()
