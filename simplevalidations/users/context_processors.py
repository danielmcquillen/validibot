import logging

from django.conf import settings

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.models import ensure_personal_workspace
from simplevalidations.users.scoping import ensure_active_org_scope

logger = logging.getLogger(__name__)


def organization_context(request):
    """
    Provide organization context for the current user.
    Sets request.active_org to the active organization.
    """

    if not hasattr(request, "user"):
        return {}

    if not hasattr(request, "session"):
        return {}

    try:
        if not request.user.is_authenticated:
            return {}
    except Exception:
        return {}

    try:
        ensure_personal_workspace(request.user)
    except Exception:
        logger.exception(
            "Failed to ensure personal workspace for user %s", request.user.id
        )

    try:
        return _apply_organization_context(request)
    except Exception:
        logger.exception(
            "Failed to apply organization context for user %s", request.user.id
        )
        return {}


def _apply_organization_context(request):
    """
    Determines the active organization for the user and
    returns relevant context variables.
    """
    memberships_qs = (
        request.user.memberships.filter(is_active=True)
        .select_related("org")
        .order_by("org__name")
    )
    memberships, active_org, active_membership = ensure_active_org_scope(
        request,
        memberships_qs,
    )
    is_org_admin = bool(active_membership and active_membership.is_admin)
    can_manage_validators = bool(
        active_membership
        and (
            active_membership.is_admin
            or active_membership.has_role(RoleCode.AUTHOR)
            or active_membership.has_role(RoleCode.OWNER)
        )
    )

    return {
        "org_memberships": memberships,
        "active_org": active_org,
        "active_membership": active_membership,
        "is_org_admin": is_org_admin,
        "can_manage_validators": can_manage_validators,
    }


def allauth_settings(request):
    """Expose some settings from django-allauth in templates."""
    return {
        "ACCOUNT_ALLOW_REGISTRATION": settings.ACCOUNT_ALLOW_REGISTRATION,
    }
