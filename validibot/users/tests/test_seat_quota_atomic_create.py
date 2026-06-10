"""Regression tests: ``create_membership_with_seat_check`` is concurrency-safe.

WHY THIS SUITE EXISTS
---------------------
Adding a paid seat used to be a check-then-create with a time-of-check-to-
time-of-use (TOCTOU) gap: ``check_org_seat_quota(org)`` ran a non-locking
``COUNT`` and the ``Membership`` row was inserted later, so two requests that
both observed ``current == cap - 1`` could each pass the count and both INSERT —
pushing a Pro org one seat over the cap it is billed against. Both
seat-consuming paths (the direct-add form's ``save`` and
:meth:`~validibot.users.models.MemberInvite.accept`) now funnel through
:func:`validibot.users.seats.create_membership_with_seat_check`, which takes a
row lock on the Organization and re-checks the cap *inside* the transaction.

These tests pin the functional contract (cap enforced at the boundary, the
happy path below the cap still works, an existing member consumes no seat) and
that the helper actually issues a ``SELECT ... FOR UPDATE`` row lock — the
mechanism that serialises concurrent adds. We synthesise a Pro-equivalent
license with ``set_license`` rather than depending on the ``validibot-pro``
wheel, mirroring ``test_seat_quota.py``.
"""

from __future__ import annotations

import pytest

from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import set_license
from validibot.users.models import Membership
from validibot.users.seats import SeatQuotaExceededError
from validibot.users.seats import create_membership_with_seat_check
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory

# Pro's contractual seat count used throughout this module. Named so the
# at-cap / under-cap arithmetic reads clearly and avoids a magic number.
PRO_SEAT_CAP = 3


@pytest.fixture
def restore_community_license():
    """Reset the module-level license after each test.

    ``set_license`` mutates a process global; without this teardown a test that
    activates Pro would leak the seat cap into unrelated tests and produce
    confusing cross-test failures.
    """
    yield
    set_license(License(edition=Edition.COMMUNITY))


def _activate_pro_with_seat_cap(cap: int) -> None:
    """Synthesise a Pro license carrying only a seat cap.

    The feature set is deliberately empty: the behaviour under test is seat
    enforcement, not feature gating.
    """
    set_license(
        License(
            edition=Edition.PRO,
            features=frozenset(),
            max_members_per_org=cap,
        ),
    )


@pytest.mark.django_db
def test_creates_membership_below_cap(restore_community_license):
    """Below the cap the helper creates a new active membership.

    Pins the happy path so the concurrency guard cannot be "fixed" by refusing
    every add — a legitimate seat below the cap must still be granted.
    """
    _activate_pro_with_seat_cap(PRO_SEAT_CAP)
    org = OrganizationFactory()
    user = UserFactory(orgs=[])

    membership, created = create_membership_with_seat_check(org=org, user=user)

    assert created is True
    assert membership.is_active is True
    assert Membership.objects.filter(org=org, user=user, is_active=True).exists()


@pytest.mark.django_db
def test_refuses_at_cap_and_creates_no_membership(restore_community_license):
    """At the cap the helper raises and writes no membership row.

    This is the heart of the TOCTOU fix: the re-check runs under the row lock,
    so a request arriving when the org is already full is refused rather than
    over-filling it. We assert both the exception and the absence of a side
    effect (a half-applied membership would itself be the seat-cap breach).
    """
    _activate_pro_with_seat_cap(PRO_SEAT_CAP)
    org = OrganizationFactory()
    for _ in range(PRO_SEAT_CAP):
        Membership.objects.create(user=UserFactory(orgs=[]), org=org, is_active=True)

    new_user = UserFactory(orgs=[])

    with pytest.raises(SeatQuotaExceededError):
        create_membership_with_seat_check(org=org, user=new_user)

    assert not Membership.objects.filter(org=org, user=new_user).exists()


@pytest.mark.django_db
def test_existing_active_member_consumes_no_seat(restore_community_license):
    """Re-running the helper for an existing active member must not refuse.

    A user who is already an active member consumes no new seat, so the quota
    check is skipped for them — otherwise a re-invite or role update of an
    existing member would be wrongly blocked once the org hit its cap. We put
    the org exactly at cap (the existing member being one of those seats) and
    assert the helper returns the existing row without raising.
    """
    _activate_pro_with_seat_cap(PRO_SEAT_CAP)
    org = OrganizationFactory()
    existing = UserFactory(orgs=[])
    Membership.objects.create(user=existing, org=org, is_active=True)
    # Fill the remaining seats so the org sits exactly at its cap.
    for _ in range(PRO_SEAT_CAP - 1):
        Membership.objects.create(user=UserFactory(orgs=[]), org=org, is_active=True)

    membership, created = create_membership_with_seat_check(org=org, user=existing)

    assert created is False
    assert membership.user == existing


@pytest.mark.django_db
def test_takes_row_lock_on_org(restore_community_license):
    """The check-and-create must run under a ``SELECT ... FOR UPDATE`` row lock.

    The lock is the mechanism that serialises concurrent seat-consuming
    requests: without it the functional cap check is still racy. A purely
    behavioural test cannot observe the race deterministically, so we assert the
    helper issues the row lock on the Organization — the change that actually
    closes the window. Guarded to PostgreSQL because ``select_for_update`` is a
    silent no-op on backends without row locking.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    if connection.vendor != "postgresql":
        pytest.skip("select_for_update row locking requires PostgreSQL")

    _activate_pro_with_seat_cap(PRO_SEAT_CAP)
    org = OrganizationFactory()
    user = UserFactory(orgs=[])

    with CaptureQueriesContext(connection) as ctx:
        create_membership_with_seat_check(org=org, user=user)

    sql = " ".join(q["sql"].lower() for q in ctx.captured_queries)
    assert "for update" in sql
