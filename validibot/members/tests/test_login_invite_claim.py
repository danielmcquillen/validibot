"""Tests for the login-time rescue of stranded email-only member invites.

Member-invite acceptance normally rides on a browser-session token: the
invitee clicks the emailed link and signs up in the *same* browser, where the
allauth ``AccountAdapter`` redeems the token. A user who gets an account any
other way — signing up directly without the link, or *already* having an
account when invited by email — never carries that token, so the invite is
left ``PENDING`` with ``invitee_user`` unset and the invitee has no in-app way
to find it. That is the "invited, signed up, still shows Pending" bug.

The fix is a single ``user_logged_in`` receiver
(:func:`validibot.users.signals.claim_member_invites_on_login`) backed by
:func:`validibot.members.views.claim_pending_member_invites_for_user`: at
login it binds any matching pending invite to the account and creates the same
``MEMBER_INVITE`` notification a user-targeted invite would have gotten, so the
existing notification UI can offer one-click Accept/Decline.

These tests matter because this code gates organization access. The two
behaviours we must never regress are (1) a real invitee can finally see and
accept their invite, and (2) we *surface* rather than *auto-grant* — joining an
org stays an explicit choice, and we never claim an expired invite or one
addressed to someone else.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.signals import user_logged_in
from django.test import RequestFactory
from django.utils import timezone

from validibot.core.constants import InviteStatus
from validibot.members.views import claim_pending_member_invites_for_user
from validibot.notifications.models import Notification
from validibot.users.models import MemberInvite
from validibot.users.models import Membership
from validibot.users.signals import claim_member_invites_on_login
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _make_invite(org, inviter, email, *, roles=None, expires_in_days=7):
    """Create an email-only (``invitee_user`` unset) pending member invite.

    Mirrors how :class:`~validibot.users.forms.InviteUserForm` persists an
    invite to an address with no Validibot account yet — the exact case the
    login rescue exists to handle.
    """
    return MemberInvite.objects.create(
        org=org,
        inviter=inviter,
        invitee_email=email,
        roles=roles or [],
        expires_at=timezone.now() + timedelta(days=expires_in_days),
    )


def _attach_messages(request):
    """Attach a session + fallback message storage to a bare factory request.

    This project's ``FallbackStorage`` includes the session-backed store, which
    eagerly requires ``request.session``; a ``RequestFactory`` request has none,
    so we give it one before wiring up messages.
    """
    from django.contrib.messages.storage.fallback import FallbackStorage
    from django.contrib.sessions.backends.db import SessionStore

    request.session = SessionStore()
    request._messages = FallbackStorage(request)


# ── The headline bug: an existing account never saw its emailed invite ──────
def test_login_claims_pending_email_only_invite():
    """The core fix: a user whose email matches a pending invite gets bound.

    This is the exact scenario from the bug report — the person was invited by
    email, signed up / already had an account, but the invite stayed Pending
    because nothing matched it to them. After the claim it is bound to their
    account and surfaced as a notification, ready to accept.
    """
    org = OrganizationFactory()
    inviter = UserFactory(orgs=[org])
    invitee = UserFactory(email="invitee@example.com")
    invite = _make_invite(
        org, inviter, "invitee@example.com", roles=["WORKFLOW_VIEWER"]
    )
    assert invite.invitee_user_id is None

    claimed = claim_pending_member_invites_for_user(invitee)

    assert [c.id for c in claimed] == [invite.id]
    invite.refresh_from_db()
    assert invite.invitee_user_id == invitee.id
    # A notification now carries the in-app Accept/Decline actions.
    assert Notification.objects.filter(
        user=invitee,
        member_invite=invite,
        type=Notification.Type.MEMBER_INVITE,
    ).exists()


def test_claim_surfaces_but_does_not_auto_accept():
    """We *surface* the invite; joining stays an explicit choice.

    Auto-joining a user to an org just because they signed up would grant
    access without consent. The claim must leave the invite ``PENDING`` and
    create no ``Membership`` — that only happens when the user clicks Accept.
    """
    org = OrganizationFactory()
    inviter = UserFactory(orgs=[org])
    invitee = UserFactory(email="invitee@example.com")
    invite = _make_invite(org, inviter, "invitee@example.com")

    claim_pending_member_invites_for_user(invitee)

    invite.refresh_from_db()
    assert invite.status == InviteStatus.PENDING
    assert not Membership.objects.filter(user=invitee, org=org).exists()


# ── Matching rules ──────────────────────────────────────────────────────────
def test_email_match_is_case_insensitive():
    """Invitee emails differ in case across systems; the match must not care.

    An invite to ``User@Example.com`` must be claimed by an account whose
    stored email is ``user@example.com`` (or vice versa), or real invitees
    would be silently stranded over a capitalization mismatch.
    """
    org = OrganizationFactory()
    inviter = UserFactory(orgs=[org])
    invitee = UserFactory(email="user@example.com")
    invite = _make_invite(org, inviter, "User@Example.com")

    claimed = claim_pending_member_invites_for_user(invitee)

    assert [c.id for c in claimed] == [invite.id]


def test_invite_for_a_different_email_is_left_alone():
    """We must never claim an invite addressed to someone else.

    The match is the only authorization here, so a non-matching email must
    yield no claim — otherwise logging in could hijack another person's invite.
    """
    org = OrganizationFactory()
    inviter = UserFactory(orgs=[org])
    invitee = UserFactory(email="me@example.com")
    other_invite = _make_invite(org, inviter, "someone-else@example.com")

    claimed = claim_pending_member_invites_for_user(invitee)

    assert claimed == []
    other_invite.refresh_from_db()
    assert other_invite.invitee_user_id is None


def test_user_with_no_email_claims_nothing():
    """A blank account email must never match the (also-blank) invite default.

    ``MemberInvite.invitee_email`` is ``blank=True``; an empty-string match
    would let an email-less account vacuum up every malformed invite. Guard it.
    """
    org = OrganizationFactory()
    inviter = UserFactory(orgs=[org])
    invitee = UserFactory(email="")
    _make_invite(org, inviter, "")

    assert claim_pending_member_invites_for_user(invitee) == []


def test_multiple_pending_invites_are_all_claimed():
    """A person may be invited to several orgs; each invite must be surfaced.

    Claiming only the first would leave the others stranded — so all matching
    pending invites are bound and each gets its own notification.
    """
    inviter = UserFactory()
    org_a = OrganizationFactory()
    org_b = OrganizationFactory()
    invitee = UserFactory(email="multi@example.com")
    invite_a = _make_invite(org_a, inviter, "multi@example.com")
    invite_b = _make_invite(org_b, inviter, "multi@example.com")

    claimed = claim_pending_member_invites_for_user(invitee)

    assert {c.id for c in claimed} == {invite_a.id, invite_b.id}
    # Each invite gets its own notification — assert *which*, not just how many.
    notified_invite_ids = set(
        Notification.objects.filter(
            user=invitee,
            type=Notification.Type.MEMBER_INVITE,
        ).values_list("member_invite_id", flat=True),
    )
    assert notified_invite_ids == {invite_a.id, invite_b.id}


# ── Idempotency and lifecycle guards ────────────────────────────────────────
def test_claim_is_idempotent_across_repeated_logins():
    """Logging in twice must not bind twice or spawn duplicate notifications.

    Only invites with no ``invitee_user`` are eligible, so once the first
    login binds the invite the second login is a clean no-op. Without this a
    user would accumulate a fresh notification on every single login.
    """
    org = OrganizationFactory()
    inviter = UserFactory(orgs=[org])
    invitee = UserFactory(email="invitee@example.com")
    invite = _make_invite(org, inviter, "invitee@example.com")

    first = claim_pending_member_invites_for_user(invitee)
    second = claim_pending_member_invites_for_user(invitee)

    assert [c.id for c in first] == [invite.id]
    assert second == []
    assert Notification.objects.filter(user=invitee, member_invite=invite).count() == 1


def test_already_bound_invite_is_not_reclaimed():
    """An invite that already has an ``invitee_user`` is out of scope.

    Those came in through the normal user-targeted path (which already made a
    notification at creation), so re-claiming them would duplicate the surface.
    """
    org = OrganizationFactory()
    inviter = UserFactory(orgs=[org])
    invitee = UserFactory(email="invitee@example.com")
    invite = _make_invite(org, inviter, "invitee@example.com")
    invite.invitee_user = invitee
    invite.save(update_fields=["invitee_user", "modified"])

    assert claim_pending_member_invites_for_user(invitee) == []


def test_expired_invite_is_not_claimed():
    """An expired offer must not be revived by a late login.

    ``expires_at`` in the past means the offer has lapsed; claiming it would
    let a stale invitation grant access days after the admin expected it to
    die. The query filters on ``expires_at__gt=now`` precisely for this.
    """
    org = OrganizationFactory()
    inviter = UserFactory(orgs=[org])
    invitee = UserFactory(email="late@example.com")
    invite = _make_invite(org, inviter, "late@example.com", expires_in_days=-1)

    assert claim_pending_member_invites_for_user(invitee) == []
    invite.refresh_from_db()
    assert invite.invitee_user_id is None


def test_non_pending_invite_is_not_claimed():
    """Declined / cancelled / accepted invites must stay untouched.

    Only ``PENDING`` invites are live offers. A previously declined invite, for
    instance, must not silently resurrect when the same person logs in later.
    """
    org = OrganizationFactory()
    inviter = UserFactory(orgs=[org])
    invitee = UserFactory(email="declined@example.com")
    invite = _make_invite(org, inviter, "declined@example.com")
    invite.status = InviteStatus.DECLINED
    invite.save(update_fields=["status", "modified"])

    assert claim_pending_member_invites_for_user(invitee) == []


# ── The signal wrapper (wiring + the heads-up message) ──────────────────────
def test_login_signal_claims_and_adds_a_message():
    """Firing ``user_logged_in`` claims the invite and flashes a heads-up.

    Proves the receiver is wired to the login signal, binds the invite, and
    surfaces a message pointing the user at their notifications. The
    notification bell carries the actual Accept action; the message just makes
    the invite discoverable on the very next page.
    """
    org = OrganizationFactory()
    inviter = UserFactory(orgs=[org])
    invitee = UserFactory(email="invitee@example.com")
    invite = _make_invite(org, inviter, "invitee@example.com")

    request = RequestFactory().get("/")
    _attach_messages(request)
    user_logged_in.send(sender=invitee.__class__, request=request, user=invitee)

    invite.refresh_from_db()
    assert invite.invitee_user_id == invitee.id
    rendered = [m.message for m in request._messages]
    assert any(org.name in msg for msg in rendered)


def test_login_signal_is_a_noop_with_no_pending_invite():
    """No invite → no message and no error, even off a bare request.

    The common case (a normal login with nothing pending) must be silent and
    must not blow up when the request has message storage but nothing to say.
    """
    invitee = UserFactory(email="nobody@example.com")
    request = RequestFactory().get("/")
    _attach_messages(request)

    claim_member_invites_on_login(
        sender=invitee.__class__,
        request=request,
        user=invitee,
    )

    assert list(request._messages) == []
