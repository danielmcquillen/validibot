"""Tests for the seat-quota gate on paid editions.

Pro is a 3-seat tier; Community and Enterprise are uncapped. The
quota gate lives in :mod:`validibot.users.seats` and is wired into
:meth:`MemberInvite.accept`. Tests here exercise both the helper
in isolation (against a synthesised license) and through the
invite-accept flow (the real production callsite).

Why both layers: the helper test pins the rule itself ("4th member
is refused when cap is 3") so a refactor can't quietly weaken the
gate; the invite-accept test pins the *integration* ("paying
customer hits a clean error message at the point of friction") so a
future change to ``accept`` can't accidentally drop the check.

The tests synthesise a Pro-equivalent license with ``set_license``
rather than depending on ``validibot-pro`` being installed —
community CI doesn't always have the Pro package available, and
testing the rule should not require the commercial wheel.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from validibot.core.constants import InviteStatus
from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import set_license
from validibot.users.constants import RoleCode
from validibot.users.models import MemberInvite
from validibot.users.models import Membership
from validibot.users.seats import SeatQuotaExceededError
from validibot.users.seats import check_org_seat_quota
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory


@pytest.fixture
def restore_community_license():
    """Reset the module-level license after each test.

    ``set_license`` mutates a module global. Without this fixture a
    test that activates Pro would leak into the next test in the
    suite, producing confusing cross-test interactions.
    """
    yield
    set_license(License(edition=Edition.COMMUNITY))


def _activate_pro_with_seat_cap(cap: int) -> None:
    """Synthesise a Pro license with the given seat cap.

    Uses an empty feature set deliberately — the rule under test is
    seat enforcement, not feature gating. A real Pro license would
    carry feature flags too, but adding them here would couple this
    test file to the Pro feature set and make every Pro feature
    addition cause a failure here.
    """
    set_license(
        License(
            edition=Edition.PRO,
            features=frozenset(),
            max_members_per_org=cap,
        ),
    )


# =============================================================================
# Helper: ``check_org_seat_quota``
# =============================================================================


@pytest.mark.django_db
def test_seat_quota_noop_under_community_license():
    """Community has no seat cap, so the helper is a no-op even at scale.

    Important because community must be a no-cost edition — adding a
    seat counter that ever raises would silently break self-hosted
    community deployments running large teams.
    """
    org = OrganizationFactory()
    for _ in range(5):
        Membership.objects.create(user=UserFactory(), org=org, is_active=True)

    # No exception, no return value — silent success is the contract.
    assert check_org_seat_quota(org) is None


@pytest.mark.django_db
def test_seat_quota_noop_under_enterprise_license(restore_community_license):
    """Enterprise leaves ``max_members_per_org=None`` → uncapped.

    A regression here would mean Enterprise customers (the most
    expensive tier) suddenly hit a seat cap they were never sold.
    """
    set_license(License(edition=Edition.ENTERPRISE))
    org = OrganizationFactory()
    for _ in range(10):
        Membership.objects.create(user=UserFactory(), org=org, is_active=True)

    assert check_org_seat_quota(org) is None


@pytest.mark.django_db
def test_seat_quota_under_cap_passes(restore_community_license):
    """A Pro org with 2 of 3 seats used must let one more through.

    The boundary case: at the cap, the next attempt is refused; below
    the cap, it succeeds. Pinning both sides protects against
    off-by-one errors swapping ``>=`` for ``>``.
    """
    _activate_pro_with_seat_cap(3)
    org = OrganizationFactory()
    Membership.objects.create(user=UserFactory(), org=org, is_active=True)
    Membership.objects.create(user=UserFactory(), org=org, is_active=True)

    assert check_org_seat_quota(org) is None


@pytest.mark.django_db
def test_seat_quota_at_cap_raises(restore_community_license):
    """A Pro org at exactly the cap must refuse the next seat.

    The error must surface ``current`` and ``max`` so the calling
    view can render a precise message — vague "you're over your
    limit" wording is the kind of thing customers escalate to
    support, costing time on both sides.
    """
    _activate_pro_with_seat_cap(3)
    org = OrganizationFactory()
    for _ in range(3):
        Membership.objects.create(user=UserFactory(), org=org, is_active=True)

    with pytest.raises(SeatQuotaExceededError) as excinfo:
        check_org_seat_quota(org)

    pro_seat_cap = 3
    assert excinfo.value.current_seats == pro_seat_cap
    assert excinfo.value.max_seats == pro_seat_cap
    assert excinfo.value.org == org


@pytest.mark.django_db
def test_seat_quota_inactive_memberships_dont_count(restore_community_license):
    """Deactivated members free their seat.

    The "remove a member to free a seat" upgrade-path message in the
    error string only makes sense if removing a member actually
    frees the seat. This test pins the soft-delete semantics — a
    membership with ``is_active=False`` does not consume a seat and
    a new invite can be accepted in its place.
    """
    _activate_pro_with_seat_cap(3)
    org = OrganizationFactory()

    # Three active seats: at cap.
    actives = [
        Membership.objects.create(user=UserFactory(), org=org, is_active=True)
        for _ in range(3)
    ]
    # Soft-deactivate one — seat should free up.
    actives[0].is_active = False
    actives[0].save(update_fields=["is_active"])

    assert check_org_seat_quota(org) is None


# =============================================================================
# Integration: invite-accept respects the quota
# =============================================================================


def _make_pending_invite(org, invitee):
    """Build a one-day-valid pending member invite for the given user.

    The fixture-style helper exists so the test bodies can focus on
    the *quota* assertion rather than the boilerplate of constructing
    a valid invite. Mirrors the shape used by the existing invite
    tests in ``test_invites.py``.
    """
    return MemberInvite.create_with_expiry(
        org=org,
        inviter=UserFactory(),
        invitee_user=invitee,
        invitee_email=invitee.email,
        roles=[RoleCode.WORKFLOW_VIEWER],
        expires_at=timezone.now() + timedelta(days=1),
    )


@pytest.mark.django_db
def test_invite_accept_refuses_when_org_at_seat_cap(restore_community_license):
    """The seat-cap rule fires at the invite-accept step.

    This is the real production callsite — without this test, the
    rule could be silently bypassed by a refactor of ``accept`` and
    the helper test alone wouldn't catch it.
    """
    _activate_pro_with_seat_cap(3)
    org = OrganizationFactory()
    for _ in range(3):
        Membership.objects.create(user=UserFactory(), org=org, is_active=True)

    invite = _make_pending_invite(org, UserFactory())
    with pytest.raises(SeatQuotaExceededError):
        invite.accept()
    # The invite stays pending so the inviter can retry once a seat
    # is freed — flipping it to ACCEPTED on a refused accept would
    # be both wrong (no membership exists) and confusing
    # (operator sees "accepted" but no member appears).
    assert invite.status == InviteStatus.PENDING


@pytest.mark.django_db
def test_invite_accept_succeeds_when_under_cap(restore_community_license):
    """A Pro org under its cap accepts new invites normally.

    Symmetric counterpart to the above — proves the quota gate
    doesn't break the happy path.
    """
    _activate_pro_with_seat_cap(3)
    org = OrganizationFactory()
    Membership.objects.create(user=UserFactory(), org=org, is_active=True)

    invitee = UserFactory()
    invite = _make_pending_invite(org, invitee)

    membership = invite.accept()

    assert invite.status == InviteStatus.ACCEPTED
    assert membership.user == invitee


@pytest.mark.django_db
def test_invite_accept_for_existing_member_is_not_a_new_seat(
    restore_community_license,
):
    """Re-inviting an existing member at the cap must succeed.

    Without the "already member?" check in ``accept``, a re-invite
    flow at the cap would falsely refuse a no-op (the user is
    already in the org). This test pins that exemption — it's a
    surprisingly easy regression and one a customer would notice
    quickly.
    """
    _activate_pro_with_seat_cap(3)
    org = OrganizationFactory()
    existing_users = [UserFactory() for _ in range(3)]
    for user in existing_users:
        Membership.objects.create(user=user, org=org, is_active=True)

    # Re-invite an existing member — seat count would not change.
    invite = _make_pending_invite(org, existing_users[0])
    membership = invite.accept()

    assert invite.status == InviteStatus.ACCEPTED
    assert membership.user == existing_users[0]
