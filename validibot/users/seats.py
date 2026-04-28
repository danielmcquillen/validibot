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
    from validibot.users.models import Organization

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
