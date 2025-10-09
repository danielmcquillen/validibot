from django.conf import settings

from simplevalidations.users.models import ensure_personal_workspace


def organization_context(request):
    if not request.user.is_authenticated:
        return {}

    ensure_personal_workspace(request.user)

    memberships = list(
        request.user.memberships.filter(is_active=True)
        .select_related("org")
        .order_by("org__name")
    )

    session_org_id = request.session.get("active_org_id")
    active_org = None
    active_membership = None

    if session_org_id:
        active_membership = next(
            (m for m in memberships if m.org_id == session_org_id),
            None,
        )
        if active_membership:
            active_org = active_membership.org

    if not active_org and getattr(request.user, "current_org", None):
        active_org = request.user.current_org
        active_membership = next(
            (m for m in memberships if m.org == active_org),
            None,
        )

    if not active_org and memberships:
        active_membership = memberships[0]
        active_org = active_membership.org

    if active_org:
        request.active_org = active_org
        request.session["active_org_id"] = active_org.id
        if getattr(request.user, "current_org_id", None) != active_org.id:
            request.user.set_current_org(active_org)

    is_org_admin = bool(active_membership and active_membership.is_admin)

    return {
        "org_memberships": memberships,
        "active_org": active_org,
        "active_membership": active_membership,
        "is_org_admin": is_org_admin,
    }


def allauth_settings(request):
    """Expose some settings from django-allauth in templates."""
    return {
        "ACCOUNT_ALLOW_REGISTRATION": settings.ACCOUNT_ALLOW_REGISTRATION,
    }
