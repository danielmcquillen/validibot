"""Regression tests: ``OrganizationMemberForm`` honours the seat-quota gate.

``OrganizationMemberForm`` lets an org admin add an *existing* user as an
active member directly by email, without going through the invite/accept
handshake. That direct-add path creates a new active
:class:`~validibot.users.models.Membership`, so it must respect the paid-edition
seat cap exactly as :meth:`MemberInvite.accept` does. Historically the form's
``clean``/``save`` skipped :func:`validibot.users.seats.check_org_seat_quota`,
which meant a Pro org at its seat cap could be grown past the cap simply by
typing another user's email into this form — a quiet bypass of the seat limit
the customer is being billed against.

These tests pin the fix at the boundary: at the cap the form is invalid with an
actionable error and *no* membership is created; one seat below the cap the form
still validates so the happy path is not broken. We synthesise a Pro-equivalent
license with ``set_license`` rather than depending on the ``validibot-pro`` wheel
being installed, mirroring ``test_seat_quota.py``.
"""

from __future__ import annotations

import pytest

from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import set_license
from validibot.users.forms import OrganizationMemberForm
from validibot.users.models import Membership
from validibot.users.tests.factories import OrganizationFactory
from validibot.users.tests.factories import UserFactory

# Pro's contractual seat count used throughout this module. Named so the
# at-cap / under-cap arithmetic reads clearly and avoids a magic number.
PRO_SEAT_CAP = 3


@pytest.fixture
def restore_community_license():
    """Reset the module-level license after each test.

    ``set_license`` mutates a process global; without this teardown a test
    that activates Pro would leak the seat cap into unrelated tests and
    produce confusing cross-test failures.
    """
    yield
    set_license(License(edition=Edition.COMMUNITY))


def _activate_pro_with_seat_cap(cap: int) -> None:
    """Synthesise a Pro license carrying only a seat cap.

    The feature set is deliberately empty: the behaviour under test is seat
    enforcement, not feature gating, and coupling this file to the real Pro
    feature flags would make every future Pro feature addition break here.
    """
    set_license(
        License(
            edition=Edition.PRO,
            features=frozenset(),
            max_members_per_org=cap,
        ),
    )


@pytest.mark.django_db
def test_direct_add_at_seat_cap_is_invalid_and_creates_no_membership(
    restore_community_license,
):
    """Adding a member to a Pro org already at its cap must fail validation.

    This is the core of the ``seat-quota-direct-add`` fix: the direct-add form
    is the one membership-creating path that bypassed ``MemberInvite.accept``,
    so it must run the same seat-quota gate. The test asserts both that the form
    is invalid (so the view re-renders with an error instead of silently
    over-filling the org) and that no stray ``Membership`` row was written.
    """
    _activate_pro_with_seat_cap(PRO_SEAT_CAP)
    org = OrganizationFactory()
    for _ in range(PRO_SEAT_CAP):
        Membership.objects.create(user=UserFactory(), org=org, is_active=True)

    new_user = UserFactory()
    form = OrganizationMemberForm(
        data={"email": new_user.email, "roles": []},
        organization=org,
    )

    assert not form.is_valid()
    # The error carries the helper's precise "N of M seats" guidance so the
    # admin knows to free a seat or upgrade — vague wording drives support load.
    assert "seat cap" in " ".join(form.errors.get("__all__", []))
    # The validation failure must not have created a membership as a side effect.
    assert not Membership.objects.filter(user=new_user, org=org).exists()


@pytest.mark.django_db
def test_direct_add_under_seat_cap_still_validates(restore_community_license):
    """One seat below the cap, the direct-add form must still validate.

    Pins the boundary on the permissive side so the seat-quota guard cannot be
    "fixed" by simply rejecting every direct add — legitimate adds below the cap
    must keep working. Protects against an off-by-one (``>`` vs ``>=``) swap.
    """
    _activate_pro_with_seat_cap(PRO_SEAT_CAP)
    org = OrganizationFactory()
    for _ in range(PRO_SEAT_CAP - 1):
        Membership.objects.create(user=UserFactory(), org=org, is_active=True)

    new_user = UserFactory()
    form = OrganizationMemberForm(
        data={"email": new_user.email, "roles": []},
        organization=org,
    )

    assert form.is_valid(), form.errors
