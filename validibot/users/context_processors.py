import logging

from django.conf import settings

from validibot.billing.constants import PlanCode
from validibot.users.constants import PermissionCode
from validibot.users.models import ensure_personal_workspace
from validibot.users.scoping import ensure_active_org_scope

logger = logging.getLogger(__name__)


def organization_context(request):
    """
    Provide organization context for the current user.
    Sets request.active_org to the active organization.

    For workflow guests (users with only WorkflowAccessGrants, no org memberships),
    returns a minimal context with is_workflow_guest=True.
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

    # Check if user is a workflow guest (has grants but no memberships).
    # Must be checked BEFORE ensure_personal_workspace which would create one.
    if request.user.is_workflow_guest:
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
    """
    Return context for workflow guests.

    Workflow guests have access to shared workflows via WorkflowAccessGrant,
    but no organization membership. They see a limited UI with only their
    shared workflows and validation runs.
    """
    return {
        "is_workflow_guest": True,
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
            "is_workflow_guest": False,
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

    # Determine if we should show limited navigation.
    # Free plan users viewing their personal org see only Workflows + Validation Runs.
    show_limited_nav = False
    if active_org and active_org.is_personal:
        subscription = getattr(active_org, "subscription", None)
        plan = getattr(subscription, "plan", None) if subscription else None
        if plan and plan.code == PlanCode.FREE:
            show_limited_nav = True

    return {
        "is_workflow_guest": False,
        "show_limited_nav": show_limited_nav,
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


def signup_plan_context(request):
    """
    Provide selected plan context for signup/login pages.

    When users come from the pricing page with a ?plan= parameter,
    this captures the plan info and makes it available in templates
    to show "You selected the Team plan" messaging.
    """
    from validibot.users.adapters import SELECTED_PLAN_SESSION_KEY
    from validibot.users.adapters import get_selected_plan_from_session

    # Capture plan from URL if present (GET request to signup page)
    plan_code = request.GET.get("plan")
    if plan_code:
        request.session[SELECTED_PLAN_SESSION_KEY] = plan_code

    # Get plan details from session for template
    selected_plan = get_selected_plan_from_session(request)

    return {
        "signup_selected_plan": selected_plan,
        "signup_selected_plan_code": request.session.get(SELECTED_PLAN_SESSION_KEY),
    }
