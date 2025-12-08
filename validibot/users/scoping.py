from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from django.http import HttpRequest

    from validibot.users.models import Membership
    from validibot.users.models import Organization

logger = logging.getLogger(__name__)


def ensure_active_org_scope(
    request: HttpRequest,
    memberships: Iterable[Membership] | None = None,
) -> tuple[list[Membership], Organization | None, Membership | None]:
    """
    Normalize the organization scope stored on the session and user.

    Returns:
        tuple[list[Membership], Organization | None, Membership | None]: A tuple
        containing the resolved memberships (for reuse), the active organization,
        and the matching membership (if any).
    """
    memberships_list = _materialize_memberships(request, memberships)
    session_org_id = _coerce_int(request.session.get("active_org_id"))
    active_org = None
    active_membership = None

    if session_org_id:
        active_membership = _membership_for_org(session_org_id, memberships_list)
        if active_membership:
            active_org = active_membership.org
        else:
            request.session.pop("active_org_id", None)
            logger.info(
                "Removed stale organization %s from session for user %s",
                session_org_id,
                getattr(request.user, "pk", None),
            )

    if not active_org:
        current_org = getattr(request.user, "current_org", None)
        if current_org:
            active_membership = _membership_for_org(
                current_org.id,
                memberships_list,
            )
            if active_membership:
                active_org = current_org
            else:
                request.user.current_org = None
                request.user.save(update_fields=["current_org"])
                logger.info(
                    "Cleared stale current_org %s for user %s",
                    current_org.pk,
                    getattr(request.user, "pk", None),
                )

    if not active_org and memberships_list:
        active_membership = memberships_list[0]
        active_org = active_membership.org

    if active_org:
        request.active_org = active_org
        if request.session.get("active_org_id") != active_org.id:
            request.session["active_org_id"] = active_org.id
        if getattr(request.user, "current_org_id", None) != active_org.id:
            request.user.set_current_org(active_org)

    return memberships_list, active_org, active_membership


def _materialize_memberships(
    request: HttpRequest,
    memberships: Iterable[Membership] | None,
) -> list[Membership]:
    if memberships is None:
        queryset = (
            request.user.memberships.filter(is_active=True)
            .select_related("org")
            .order_by("org__name")
        )
        return list(queryset)
    if isinstance(memberships, list):
        return memberships
    return list(memberships)


def _coerce_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _membership_for_org(
    org_id: int,
    memberships: list[Membership],
) -> Membership | None:
    for membership in memberships:
        if membership.org_id == org_id:
            return membership
    return None
