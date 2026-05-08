import logging

from django.conf import settings

from validibot.users.constants import PermissionCode
from validibot.users.constants import UserKindGroup
from validibot.users.models import ensure_personal_workspace
from validibot.users.scoping import ensure_active_org_scope

logger = logging.getLogger(__name__)


def organization_context(request):
    """Provide organization context for the current user.

    Sets ``request.active_org`` to the active organization. For accounts
    classified as ``UserKindGroup.GUEST``, returns a minimal context
    with ``user_is_guest=True`` and skips the personal-workspace
    provisioning (a guest account has no org context to provision).
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

    # Check the system-wide account kind BEFORE running
    # ensure_personal_workspace — guest accounts must not get a personal
    # workspace silently created underneath them.
    if request.user.user_kind == UserKindGroup.GUEST:
        return _guest_context(request)

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


def _guest_context(request):
    """Return template context for GUEST-classified accounts.

    GUEST accounts see a limited UI tailored to their access pattern —
    no personal org, no member-level features. Per-workflow access is
    still enforced through the usual grant machinery; this context just
    provides the chrome that surrounds it.
    """
    return {
        "user_is_guest": True,
        "show_limited_nav": True,
        "org_memberships": [],
        "active_org": None,
        "active_membership": None,
        "is_org_admin": False,
        "can_manage_validators": False,
        "has_author_admin_owner": False,
        "active_role_codes": set(),
        "has_any_org_roles": False,
    }


def _apply_organization_context(request):
    """
    Determines the active organization for the user and
    returns relevant context variables.

    Superusers get full access rights regardless of membership, allowing them
    to access all navigation and features even without explicit roles.
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

    # Superusers get full access regardless of membership
    is_superuser = request.user.is_superuser
    if is_superuser:
        return {
            "user_is_guest": False,
            "show_limited_nav": False,
            "org_memberships": memberships,
            "active_org": active_org,
            "active_membership": active_membership,
            "is_org_admin": True,
            "can_manage_validators": True,
            "has_author_admin_owner": True,
            "active_role_codes": set(),
            "has_any_org_roles": True,
        }

    active_role_codes = (
        set(active_membership.role_codes) if active_membership else set()
    )
    has_author_admin_owner = bool(
        active_org
        and request.user.has_perm(PermissionCode.WORKFLOW_EDIT.value, active_org)
    )
    is_org_admin = bool(
        active_org
        and request.user.has_perm(PermissionCode.ADMIN_MANAGE_ORG.value, active_org)
    )
    can_manage_validators = bool(
        active_org
        and request.user.has_perm(PermissionCode.VALIDATOR_EDIT.value, active_org)
    )

    return {
        "user_is_guest": False,
        "show_limited_nav": False,
        "org_memberships": memberships,
        "active_org": active_org,
        "active_membership": active_membership,
        "is_org_admin": is_org_admin,
        "can_manage_validators": can_manage_validators,
        "has_author_admin_owner": has_author_admin_owner,
        "active_role_codes": active_role_codes,
        "has_any_org_roles": bool(active_role_codes),
    }


def allauth_settings(request):
    """Expose some settings from django-allauth in templates."""
    return {
        "ACCOUNT_ALLOW_REGISTRATION": settings.ACCOUNT_ALLOW_REGISTRATION,
    }
