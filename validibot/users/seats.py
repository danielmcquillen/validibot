"""Seat-quota enforcement for paid editions.

Each paid edition declares a ``max_members_per_org`` on its
:class:`~validibot.core.license.License`. Community and Enterprise
leave it ``None`` (no cap); Pro sets it to its contractual seat
count. This module is the single place that consults that value
and decides whether one more :class:`~validibot.users.models.Membership`
row may be created in a given :class:`~validibot.users.models.Organization`.

**Why a separate module.** Seat enforcement is open-core business
logic — the rule lives in community so a self-hosted Pro deployment
gets the same cap a hosted Pro customer does. Putting it next to
``Membership`` would mix the persistence concern (model) with the
licensing concern (caller of ``get_license()``); putting it inside
``users/views.py`` would couple the rule to the HTTP layer and miss
non-HTTP callsites (management commands, admin actions, programmatic
invite acceptance). A focused module imported by every callsite is
the right shape.

**What "a seat" means.** One active :class:`Membership` row in an
:class:`Organization`. Personal-workspace orgs always have exactly
one member by construction, so the cap never bites them. Workflow
guests are a different mechanism (:class:`WorkflowAccessGrant`) and
deliberately do **not** consume a seat — see
``self-hosted-editions.md`` for the buyer-facing wording.

**When the check fires.** On every code path that creates a *new*
membership the user can pay for: today that's
:meth:`MemberInvite.accept`. Personal-workspace creation,
data-seeding management commands, and signup forms intentionally
bypass the check — they create at most one membership per user, and
gating them would lock out legitimate first-org creation.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.utils.translation import gettext_lazy as _

from validibot.core.license import get_license

if TYPE_CHECKING:
    from validibot.users.models import Membership
    from validibot.users.models import Organization
    from validibot.users.models import User

logger = logging.getLogger(__name__)


class SeatQuotaExceededError(Exception):
    """Raised when a new membership would exceed the org's seat cap.

    Carries enough context for callers (views, management commands)
    to render an actionable message — current seat count, the cap,
    and the org slug — rather than re-deriving it from the active
    license.
    """

    def __init__(
        self,
        org: Organization,
        current_seats: int,
        max_seats: int,
    ) -> None:
        self.org = org
        self.current_seats = current_seats
        self.max_seats = max_seats
        message = _(
            "Organization '%(org)s' is at its seat cap "
            "(%(current)d of %(max)d). Free a seat by removing an "
            "existing member, or upgrade to Enterprise for unlimited "
            "seats. Verifiable Credentials, signed by your "
            "deployment's key, remain available on every paid tier."
        ) % {
            "org": org.name,
            "current": current_seats,
            "max": max_seats,
        }
        super().__init__(message)


def check_org_seat_quota(org: Organization) -> None:
    """Raise :class:`SeatQuotaExceededError` if *org* is at its seat cap.

    Call this immediately before creating a new active
    :class:`Membership`. A no-op when:

    * the active license has ``max_members_per_org=None`` (Community
      and Enterprise — the cap is infinite by definition);
    * the org currently has fewer active memberships than the cap.

    Otherwise raises with the current count and cap, so the caller
    can surface a precise message to the user.
    """
    # Local import: ``Membership`` lives in ``users.models``, which
    # this module is itself imported by indirectly through the
    # invite-accept flow. Keeping the import deferred avoids a
    # potential circular-import surprise during Django app loading.
    from validibot.users.models import Membership

    cap = get_license().max_members_per_org
    if cap is None:
        return

    current = Membership.objects.filter(org=org, is_active=True).count()
    if current >= cap:
        logger.info(
            "Seat-quota refusal: org=%s current=%d cap=%d",
            org.slug,
            current,
            cap,
        )
        raise SeatQuotaExceededError(org=org, current_seats=current, max_seats=cap)


def create_membership_with_seat_check(
    *,
    org: Organization,
    user: User,
) -> tuple[Membership, bool]:
    """Create or fetch an active membership under a concurrency-safe seat check.

    This is the single chokepoint for *consuming a paid seat*. It closes a
    time-of-check-to-time-of-use (TOCTOU) race that a plain
    ``check_org_seat_quota(org)`` followed by ``Membership.objects.create(...)``
    suffers from: two requests that both observe ``current == cap - 1`` each
    pass the non-locking count and both INSERT, pushing the org one seat over
    its cap.

    The fix is to make the check-and-create atomic. We open a transaction and
    take a row lock on the :class:`~validibot.users.models.Organization`
    (``select_for_update``). A second concurrent caller blocks on that lock
    until the first commits, then its re-check below sees the updated count and
    is refused. The org row is a natural, low-contention serialization point —
    seat changes for a single org are rare and the lock is held only for the
    count-plus-insert.

    A user who is *already* an active member consumes no new seat, so the quota
    check is skipped for them; re-invite and role-update flows must not be
    refused merely because the org happens to be at capacity.

    Args:
        org: The organization gaining a member.
        user: The user to make an active member of ``org``.

    Returns:
        ``(membership, created)`` where ``created`` is ``True`` when a new row
        was inserted and ``False`` when an existing membership was returned.

    Raises:
        SeatQuotaExceededError: If creating a *new* seat would exceed the cap.
    """
    # Deferred imports mirror ``check_org_seat_quota`` — they keep this
    # licensing module free of an import-time dependency on the ORM models.
    from django.db import transaction

    from validibot.users.models import Membership
    from validibot.users.models import Organization

    with transaction.atomic():
        # Lock the org row so concurrent seat-consuming operations serialize.
        Organization.objects.select_for_update().get(pk=org.pk)
        already_active = Membership.objects.filter(
            org=org,
            user=user,
            is_active=True,
        ).exists()
        if not already_active:
            # Re-check INSIDE the lock — this is the line that makes the cap
            # hold under concurrency.
            check_org_seat_quota(org)
        return Membership.objects.get_or_create(
            org=org,
            user=user,
            defaults={"is_active": True},
        )
