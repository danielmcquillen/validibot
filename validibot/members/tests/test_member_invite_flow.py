"""Tests for the end-to-end member-invitation flow.

This suite covers the two features that turn "Invite Member" from a
half-working feature into a complete one:

1. **Tokenized acceptance + signup reconciliation.** Inviting an email
   with no Validibot account used to strand the invite: the email linked
   to ``/notifications/`` (login-walled, token-less) and nothing bound a
   pending email-only ``MemberInvite`` to the account once it signed up.
   We now mirror the guest-invite pattern — a tokenized
   ``MemberInviteAcceptView`` plus post-signup redemption in the allauth
   ``AccountAdapter`` — so a brand-new invitee can actually join.

2. **Pre-send confirmation dialog.** Before an invite is created the
   admin sees a confirmation interstitial (``InviteConfirmView``) that
   names the invitee and lists the exact permissions they'll receive. A
   *type-ahead pick* (a user the org already knows) shows that account's
   identity so a wrong-person mistake is caught; a *raw-typed address*
   stays deliberately opaque — the dialog never reveals whether an
   arbitrary email belongs to a Validibot account, so the form cannot be
   used to enumerate accounts (see ``test_invite_no_account_enumeration``).

Each test documents *why* the behaviour matters, because these flows gate
organization access and a regression would either strand invitees or
silently grant the wrong permissions.
"""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus

import pytest
from django.urls import reverse
from django.utils import timezone

from validibot.core.constants import InviteStatus
from validibot.core.features import CommercialFeature
from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import set_license
from validibot.notifications.models import Notification
from validibot.users.constants import RoleCode
from validibot.users.models import MemberInvite
from validibot.users.models import Membership
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.factories import grant_role

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _enable_team_management():
    """Activate a Pro license with TEAM_MANAGEMENT for every test here.

    The invite *create*/*confirm* views are feature-gated, so without a
    license they would 404 and the tests would assert against the wrong
    response. ``max_members_per_org`` defaults to ``None`` (no seat cap)
    so acceptance never trips a quota refusal we didn't intend. The root
    conftest autouse fixture restores the license afterwards, so this
    does not leak into other suites.
    """
    set_license(
        License(
            edition=Edition.PRO,
            features=frozenset({CommercialFeature.TEAM_MANAGEMENT.value}),
        ),
    )


@pytest.fixture
def admin_ctx(client):
    """An admin logged into an org with the active-org session primed.

    Mirrors the ``admin_client`` fixture in ``test_views.py``: the
    member-management views resolve the active org from
    ``session["active_org_id"]`` and require an org admin, so both must be
    set up for the confirm/create views to be reachable.
    """
    org = OrganizationFactory()
    admin = UserFactory(orgs=[org])
    grant_role(admin, org, RoleCode.ADMIN)
    admin.set_current_org(org)
    client.force_login(admin)
    session = client.session
    session["active_org_id"] = org.pk
    session.save()
    return client, org, admin


def _attach_messages(request):
    """Attach fallback message storage so adapter code can call messages.add."""
    from django.contrib.messages.storage.fallback import FallbackStorage

    request._messages = FallbackStorage(request)


# =============================================================================
# Confirmation dialog — InviteConfirmView
#
# The dialog is the safety gate between "fill in the form" and "actually
# grant access". It must (a) never create the invite itself, and (b)
# accurately distinguish an existing account from a brand-new email so
# the admin knows what they're authorizing.
# =============================================================================


def test_confirm_typeahead_pick_of_known_user_renders_identity_card(admin_ctx):
    """A type-ahead pick of a user the org already knows shows their identity.

    When the admin picks a *related* user (one the org has previously invited —
    the only kind the scoped type-ahead surfaces) the dialog shows *who* they're
    about to make a member, so a wrong-person mistake is caught. Identity is
    safe to show here because the org already has a relationship with that
    account, unlike a raw-typed address which stays opaque
    (``test_confirm_typed_email_of_existing_user_stays_opaque``). The
    confirmation step must also never create the ``MemberInvite`` itself.
    """
    client, org, admin = admin_ctx
    target = UserFactory()
    # Make ``target`` a user the org already knows: a prior invite is exactly
    # what the scoped type-ahead (``InviteSearchView``) surfaces, so resolving
    # the picked PK here is legitimate rather than an enumeration probe.
    MemberInvite.create_with_expiry(
        org=org,
        inviter=admin,
        invitee_user=target,
        invitee_email=target.email,
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() + timedelta(days=7),
        send_email=False,
    )

    response = client.post(
        reverse("members:invite_confirm"),
        {
            "search": target.username,
            "invitee_user": target.id,
            "invitee_email": target.email,
            "roles": [RoleCode.WORKFLOW_VIEWER],
        },
    )

    assert response.status_code == HTTPStatus.OK
    content = response.content.decode()
    # The identity-card branch rendered: the @username line and the
    # existing-account wording are both specific to ``{% if invitee_user %}``.
    assert f"@{target.username}" in content
    assert "make this user a member" in content
    # Confirmation only — the dialog persisted no NEW invite beyond the prior
    # one we created in setup.
    assert MemberInvite.objects.filter(org=org, invitee_user=target).count() == 1


def test_confirm_raw_email_renders_neutral_send_notice(admin_ctx):
    """A raw-typed address yields a neutral 'will be sent' notice, no invite yet.

    The dialog must NOT assert whether the address has an account — doing so
    would be the enumeration oracle the fix removes. It confirms only that an
    invitation will be sent (and, if there's no account yet, a sign-up link
    follows). Nothing is persisted at this stage.
    """
    client, org, _admin = admin_ctx

    response = client.post(
        reverse("members:invite_confirm"),
        {
            "search": "stranger@example.com",
            "invitee_email": "",
            "roles": [RoleCode.AUTHOR],
        },
    )

    assert response.status_code == HTTPStatus.OK
    content = response.content.decode()
    assert "An invitation will be sent to" in content
    assert "stranger@example.com" in content
    # No claim about whether the address has an account, in either direction.
    assert "does not currently have a Validibot account" not in content
    assert not MemberInvite.objects.filter(org=org).exists()


def test_confirm_typed_email_of_existing_user_stays_opaque(admin_ctx):
    """Typing an existing user's email must NOT reveal that account's identity.

    This is the account-enumeration guard at the view layer. If typing a raw
    address resolved it and the dialog showed the matched account, an admin
    could probe arbitrary emails to learn whether each has a Validibot account
    and whose it is. We type a *known* user's exact email (with no type-ahead
    pick) and assert the dialog shows only the neutral send notice — never that
    user's @username/identity card. The existing-account binding still happens,
    but server-side in ``save`` where it is not surfaced (covered by
    ``test_invite_no_account_enumeration``).
    """
    client, _org, _admin = admin_ctx
    target = UserFactory()

    response = client.post(
        reverse("members:invite_confirm"),
        {
            "search": target.email,  # typed address, no type-ahead pick
            "invitee_email": "",
            "roles": [RoleCode.WORKFLOW_VIEWER],
        },
    )

    assert response.status_code == HTTPStatus.OK
    content = response.content.decode()
    # The account exists, but the dialog must disclose neither its identity card
    # nor the existing-account wording.
    assert f"@{target.username}" not in content
    assert "make this user a member" not in content
    assert "An invitation will be sent to" in content


def test_confirm_blocks_existing_member(admin_ctx):
    """Re-inviting a current member is refused with a clear error.

    The confirmation would otherwise promise to "make this user a member"
    when they already are, and accepting would be a confusing near no-op.
    Guarding at the form keeps the dialog truthful. We make the member a known
    (previously-invited) user so the picked PK resolves under the scoped
    lookup; the active-membership check is then what blocks the re-invite.
    """
    client, org, admin = admin_ctx
    member = UserFactory(orgs=[org])
    grant_role(member, org, RoleCode.EXECUTOR)
    MemberInvite.create_with_expiry(
        org=org,
        inviter=admin,
        invitee_user=member,
        invitee_email=member.email,
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() + timedelta(days=7),
        send_email=False,
    )

    response = client.post(
        reverse("members:invite_confirm"),
        {
            "search": member.username,
            "invitee_user": member.id,
            "invitee_email": member.email,
            "roles": [RoleCode.WORKFLOW_VIEWER],
        },
    )

    assert response.status_code == HTTPStatus.OK
    assert "already a member" in response.content.decode()


def test_confirm_missing_target_rerenders_form(admin_ctx):
    """An invalid submission falls back to the editable form, not a dialog.

    ``search`` is required; submitting without it must re-render the form
    (with its validation feedback) so the admin can fix the input —
    rendering an empty confirmation would be a dead end.
    """
    client, _org, _admin = admin_ctx

    response = client.post(
        reverse("members:invite_confirm"),
        {"roles": [RoleCode.WORKFLOW_VIEWER]},
    )

    assert response.status_code == HTTPStatus.OK
    # The type-ahead results container only exists in the form partial.
    assert "invite-search-results" in response.content.decode()


# =============================================================================
# Privilege guard — OWNER is never grantable via an invitation
#
# OWNER is fixed at org setup. The checkbox is disabled in the UI, but the
# disabled attribute is presentation, not a security control — the server
# must reject a hand-crafted ``roles=[OWNER]`` POST so an admin cannot mint
# a second owner through either invite endpoint.
# =============================================================================


def test_confirm_rejects_owner_role(admin_ctx):
    """A crafted roles=[OWNER] confirm POST is refused, no dialog shown.

    Submitting OWNER must fail validation and fall back to the editable
    form (with the clear message), never reaching the confirmation dialog
    that would otherwise tee up granting org ownership.
    """
    client, org, _admin = admin_ctx
    target = UserFactory()

    response = client.post(
        reverse("members:invite_confirm"),
        {
            "search": target.username,
            "invitee_user": target.id,
            "invitee_email": target.email,
            "roles": [RoleCode.OWNER],
        },
    )

    assert response.status_code == HTTPStatus.OK
    content = response.content.decode()
    assert "Owner role cannot be assigned" in content
    # Re-rendered the editable form, not the confirmation dialog.
    assert "invite-search-results" in content
    assert "make this user a member" not in content
    assert not MemberInvite.objects.filter(org=org).exists()


def test_create_rejects_owner_role(admin_ctx):
    """A crafted roles=[OWNER] create POST persists no invite.

    Belt-and-suspenders: even a client that skips the confirmation step
    and posts straight to ``invite_create`` (the endpoint that actually
    grants access) must have OWNER rejected with no MemberInvite written.
    """
    client, org, _admin = admin_ctx
    target = UserFactory()

    response = client.post(
        reverse("members:invite_create"),
        {
            "search": target.username,
            "invitee_user": target.id,
            "invitee_email": target.email,
            "roles": [RoleCode.OWNER],
        },
    )

    assert response.status_code == HTTPStatus.OK
    assert "Owner role cannot be assigned" in response.content.decode()
    assert not MemberInvite.objects.filter(org=org).exists()


def test_confirm_allows_owner_alongside_assignable_roles_is_still_rejected(admin_ctx):
    """OWNER smuggled in beside a valid role is still rejected wholesale.

    The guard keys off *any* non-assignable code in the submission, so an
    admin cannot sneak OWNER through by pairing it with an allowed role
    like AUTHOR — the whole submission is refused.
    """
    client, org, _admin = admin_ctx
    target = UserFactory()

    response = client.post(
        reverse("members:invite_confirm"),
        {
            "search": target.username,
            "invitee_user": target.id,
            "invitee_email": target.email,
            "roles": [RoleCode.AUTHOR, RoleCode.OWNER],
        },
    )

    assert response.status_code == HTTPStatus.OK
    assert "Owner role cannot be assigned" in response.content.decode()
    assert not MemberInvite.objects.filter(org=org).exists()


# =============================================================================
# Email link — send_member_invite_email
#
# The emailed link is the invitee's only entry point, so it must be the
# tokenized accept URL (not the old login-walled /notifications/ page).
# =============================================================================


def test_invite_create_for_new_email_sends_tokenized_link(admin_ctx, mailoutbox):
    """Inviting a brand-new email sends a tokenized accept link.

    This pins the core bug fix: the email must point at
    ``member_invite_accept`` (which routes anonymous users through
    signup), never the old ``/notifications/`` URL that a brand-new
    invitee cannot use.
    """
    client, org, _admin = admin_ctx

    response = client.post(
        reverse("members:invite_create"),
        {
            "search": "fresh@example.com",
            "invitee_email": "fresh@example.com",
            "roles": [RoleCode.WORKFLOW_VIEWER],
        },
        HTTP_HX_REQUEST="true",
    )

    assert response.status_code == HTTPStatus.OK
    invite = MemberInvite.objects.get(org=org, invitee_email="fresh@example.com")
    assert invite.invitee_user is None  # email-only → email is sent
    assert len(mailoutbox) == 1
    body = mailoutbox[0].body
    accept_path = reverse("member_invite_accept", kwargs={"token": invite.token})
    assert accept_path in body
    assert "/notifications/" not in body


def test_member_page_shows_role_labels_not_codes(admin_ctx):
    """The Members page renders role *labels*, never raw enum codes.

    Regression: roles printed as the constant ("WORKFLOW_VIEWER") instead
    of the human label ("Workflow Viewer"). This covers the pending-invite
    list; the current-members table uses the same ``role_label`` filter.
    """
    client, org, admin = admin_ctx
    MemberInvite.create_with_expiry(
        org=org,
        inviter=admin,
        invitee_user=None,
        invitee_email="pending@example.com",
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() + timedelta(days=7),
    )

    response = client.get(reverse("members:member_list"))

    assert response.status_code == HTTPStatus.OK
    content = response.content.decode()
    assert str(RoleCode.WORKFLOW_VIEWER.label) in content  # "Workflow Viewer"
    assert "WORKFLOW_VIEWER" not in content


def test_invite_email_uses_inviter_display_name_not_none_none(mailoutbox):
    """The invite email shows a real inviter name and friendly role labels.

    Regression for a user-reported bug: the custom ``User`` model nulls out
    ``first_name``/``last_name``, so the inherited ``get_full_name()``
    returned the literal "None None" — and being truthy, it defeated the
    ``get_full_name() or username`` fallback, producing
    "None None has invited you ...". An inviter with no display name must
    now fall back to their username, and permissions must render as
    friendly labels ("Workflow Viewer"), not raw enum codes
    ("WORKFLOW_VIEWER").
    """
    from validibot.workflows.emails import send_member_invite_email

    org = OrganizationFactory(name="daniel's Workspace")
    inviter = UserFactory(orgs=[org], username="danielmc", name="")
    invite = MemberInvite.create_with_expiry(
        org=org,
        inviter=inviter,
        invitee_user=None,
        invitee_email="newbie@example.com",
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() + timedelta(days=7),
    )

    send_member_invite_email(invite)

    assert len(mailoutbox) == 1
    body = mailoutbox[0].body
    assert "None None" not in body
    assert "danielmc" in body
    assert str(RoleCode.WORKFLOW_VIEWER.label) in body  # "Workflow Viewer"
    assert "WORKFLOW_VIEWER" not in body


# =============================================================================
# Tokenized acceptance — MemberInviteAcceptView
# =============================================================================


def test_accept_logged_in_creates_membership_and_notifies_inviter(client):
    """A logged-in invitee who clicks the link becomes a member immediately.

    The email-only invite binds to the account that owns the invited
    email, the Membership is created with the invited roles, and the
    inviter is notified — the same outcome as the notification flow, just
    reached via the link.
    """
    org = OrganizationFactory()
    inviter = UserFactory(orgs=[org])
    grant_role(inviter, org, RoleCode.ADMIN)
    invitee = UserFactory()  # owns its own personal org, not a member of `org`

    invite = MemberInvite.create_with_expiry(
        org=org,
        inviter=inviter,
        invitee_user=None,
        invitee_email=invitee.email,
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() + timedelta(days=1),
    )

    client.force_login(invitee)
    response = client.get(
        reverse("member_invite_accept", kwargs={"token": invite.token}),
    )

    assert response.status_code == HTTPStatus.FOUND
    invite.refresh_from_db()
    assert invite.status == InviteStatus.ACCEPTED
    assert invite.invitee_user_id == invitee.id
    assert Membership.objects.filter(
        user=invitee,
        org=org,
        is_active=True,
    ).exists()
    assert Notification.objects.filter(
        user=inviter,
        org=org,
        type=Notification.Type.MEMBER_INVITE,
    ).exists()


def test_accept_anonymous_stashes_token_and_redirects_to_signup(client):
    """An anonymous click stashes the token and routes to signup.

    A brand-new invitee has no account, so the link must send them to
    signup with the token preserved in session for post-signup redemption
    — exactly the path that was missing before.
    """
    from validibot.members.views import MEMBER_INVITE_SESSION_KEY

    org = OrganizationFactory()
    inviter = UserFactory(orgs=[org])
    invite = MemberInvite.create_with_expiry(
        org=org,
        inviter=inviter,
        invitee_user=None,
        invitee_email="newbie@example.com",
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() + timedelta(days=1),
    )

    response = client.get(
        reverse("member_invite_accept", kwargs={"token": invite.token}),
    )

    assert response.status_code == HTTPStatus.FOUND
    assert reverse("account_signup") in response.url
    assert client.session.get(MEMBER_INVITE_SESSION_KEY) == str(invite.token)


def test_accept_expired_token_creates_no_membership(client):
    """An expired invite cannot be redeemed via the link.

    Expiry is the operator's promise that an invitation is time-bounded;
    the link must honour it rather than silently granting access.
    """
    org = OrganizationFactory()
    inviter = UserFactory(orgs=[org])
    invitee = UserFactory()
    invite = MemberInvite.create_with_expiry(
        org=org,
        inviter=inviter,
        invitee_user=None,
        invitee_email=invitee.email,
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() - timedelta(days=1),
    )

    client.force_login(invitee)
    response = client.get(
        reverse("member_invite_accept", kwargs={"token": invite.token}),
    )

    assert response.status_code == HTTPStatus.FOUND
    assert not Membership.objects.filter(user=invitee, org=org).exists()


def test_accept_refused_for_different_user(client):
    """An invite naming a specific user can't be redeemed by someone else.

    A link that lands in the wrong inbox (forwarded email, shared device)
    must not let an unintended account claim membership addressed to
    another user.
    """
    org = OrganizationFactory()
    inviter = UserFactory(orgs=[org])
    intended = UserFactory()
    other = UserFactory()
    invite = MemberInvite.create_with_expiry(
        org=org,
        inviter=inviter,
        invitee_user=intended,
        invitee_email=intended.email,
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() + timedelta(days=1),
    )

    client.force_login(other)
    response = client.get(
        reverse("member_invite_accept", kwargs={"token": invite.token}),
    )

    assert response.status_code == HTTPStatus.FOUND
    assert not Membership.objects.filter(user=other, org=org).exists()
    invite.refresh_from_db()
    assert invite.status == InviteStatus.PENDING


# =============================================================================
# Post-signup redemption — AccountAdapter
# =============================================================================


def test_signup_redemption_joins_org_without_guest_downgrade(rf):
    """Signing up via a member invite joins the org as a normal member.

    Unlike the guest flow, member redemption must NOT route through the
    invite-driven suppression / GUEST classification: a new member keeps
    the normal (non-guest) treatment that ``save_user`` provisions and
    *additionally* gains a membership in the inviting org. We pin two
    consequences here — the membership is created and the user is left in
    the inviting org — plus the negative: they are not reclassified GUEST.

    (Personal-workspace provisioning lives in the real ``save_user`` /
    post_save signal path, which this unit-level adapter call doesn't
    re-run; that's covered by the signup-suppression suite. The point
    here is that member redemption *adds* membership without the guest
    side effects.)
    """
    from validibot.users.adapters import MEMBER_INVITE_SESSION_KEY
    from validibot.users.adapters import AccountAdapter
    from validibot.users.constants import UserKindGroup

    org = OrganizationFactory()
    inviter = UserFactory(orgs=[org])
    grant_role(inviter, org, RoleCode.ADMIN)
    invitee = UserFactory()

    invite = MemberInvite.create_with_expiry(
        org=org,
        inviter=inviter,
        invitee_user=None,
        invitee_email=invitee.email,
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() + timedelta(days=1),
    )

    request = rf.get("/accounts/signup/")
    request.user = invitee
    request.session = {MEMBER_INVITE_SESSION_KEY: str(invite.token)}
    _attach_messages(request)

    AccountAdapter().get_signup_redirect_url(request)

    invite.refresh_from_db()
    assert invite.status == InviteStatus.ACCEPTED
    assert invite.invitee_user_id == invitee.id
    assert Membership.objects.filter(
        user=invitee,
        org=org,
        is_active=True,
    ).exists()
    invitee.refresh_from_db()
    # Dropped into the org they just joined…
    assert invitee.current_org_id == org.id
    # …and NOT downgraded to a guest the way the guest-invite flow would.
    assert invitee.user_kind != UserKindGroup.GUEST


def test_member_token_opens_otherwise_closed_registration(rf, settings):
    """A valid member token opens signup even when registration is closed.

    Closed-registration deployments (``ACCOUNT_ALLOW_REGISTRATION=False``)
    must still let an invited person create the account they were invited
    to make — the token is their authorization. A stale (expired) token
    must NOT reopen signup, or closed registration could be bypassed.
    """
    from validibot.users.adapters import MEMBER_INVITE_SESSION_KEY
    from validibot.users.adapters import AccountAdapter

    settings.ACCOUNT_ALLOW_REGISTRATION = False

    org = OrganizationFactory()
    inviter = UserFactory(orgs=[org])

    live = MemberInvite.create_with_expiry(
        org=org,
        inviter=inviter,
        invitee_user=None,
        invitee_email="live@example.com",
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() + timedelta(days=1),
    )
    request = rf.get("/accounts/signup/")
    request.session = {MEMBER_INVITE_SESSION_KEY: str(live.token)}
    assert AccountAdapter().is_open_for_signup(request) is True

    stale = MemberInvite.create_with_expiry(
        org=org,
        inviter=inviter,
        invitee_user=None,
        invitee_email="stale@example.com",
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() - timedelta(days=1),
    )
    request_stale = rf.get("/accounts/signup/")
    request_stale.session = {MEMBER_INVITE_SESSION_KEY: str(stale.token)}
    assert AccountAdapter().is_open_for_signup(request_stale) is False
