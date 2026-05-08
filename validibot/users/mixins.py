from __future__ import annotations

from django.contrib.auth.mixins import AccessMixin
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404

from validibot.users.models import Organization
from validibot.users.permissions import PermissionCode
from validibot.users.scoping import ensure_active_org_scope


class OrgMixin:
    """
    Mixin providing self.org from the request's active organization.

    Requires that ensure_active_org_scope has been called (typically by the
    organization_context context processor) to set request.active_org.

    Usage:
        class MyView(LoginRequiredMixin, OrgMixin, TemplateView):
            def get_context_data(self, **kwargs):
                context = super().get_context_data(**kwargs)
                context["subscription"] = self.org.subscription
                return context
    """

    org = None

    def dispatch(self, request, *args, **kwargs):
        """Set self.org from request.active_org."""
        # Call ensure_active_org_scope to set request.active_org
        ensure_active_org_scope(request)
        self.org = getattr(request, "active_org", None)
        return super().dispatch(request, *args, **kwargs)


class OrganizationPermissionRequiredMixin(LoginRequiredMixin):
    """Gate a view on an org-scoped Django permission.

    Subclasses set ``required_org_permission`` to a :class:`PermissionCode`
    member; this mixin resolves the target organization (URL kwarg →
    session → ``request.user.current_org``) and calls
    ``request.user.has_perm(perm, organization)`` against the
    :class:`~validibot.users.permissions.OrgPermissionBackend`.

    Composition follows Django's standard mixin pattern (:class:`LoginRequiredMixin`
    first, permission gate second, view body last). For the common
    "must be an org admin" case, use :class:`OrganizationAdminRequiredMixin`
    instead — it's a thin alias defaulting to ``ADMIN_MANAGE_ORG``.

    On success the resolved organization is stored as
    ``self.<organization_context_attr>``, the user's membership as
    ``self.organization_membership``, and the active-org session key is
    refreshed so downstream views and the org-context processor see the
    same org.
    """

    organization_lookup_kwarg = "pk"
    organization_context_attr = "managed_organization"
    # Subclass MUST set this to a PermissionCode member.
    required_org_permission: PermissionCode

    def dispatch(self, request, *args, **kwargs):
        if not hasattr(self, "required_org_permission"):
            raise NotImplementedError(
                "OrganizationPermissionRequiredMixin subclasses must set "
                "required_org_permission to a PermissionCode value."
            )

        memberships, _, _ = ensure_active_org_scope(request)
        organization = self.get_organization()
        if organization is None:
            raise PermissionDenied("Organization context is required.")

        membership = next(
            (m for m in memberships if m.org_id == organization.id),
            None,
        )
        if not membership or not request.user.has_perm(
            self.required_org_permission.value,
            organization,
        ):
            raise PermissionDenied(
                "You do not have the required access to this organization."
            )

        setattr(self, self.organization_context_attr, organization)
        self.organization_membership = membership
        request.active_org = organization
        request.session["active_org_id"] = organization.id

        return super().dispatch(request, *args, **kwargs)

    def get_organization(self) -> Organization | None:
        """Resolve the target organization for this request.

        Resolution order:

        1. ``self.object`` if it's already populated (e.g. ``DetailView``)
           and is either an :class:`Organization` or has an ``org``
           attribute.
        2. URL kwarg named by ``organization_lookup_kwarg`` (default
           ``"pk"``) — get_object_or_404.
        3. ``request.session["active_org_id"]`` — the active-org cookie
           refreshed by org-scoped middleware.
        4. ``request.active_org`` — set by upstream context processors.
        5. ``request.user.current_org`` — last-resort default for the
           common single-org case.

        Returning ``None`` means the dispatch path will raise
        ``PermissionDenied`` for "Organization context is required."
        """

        if hasattr(self, "object") and getattr(self, "object", None):
            obj = self.object
            if isinstance(obj, Organization):
                return obj
            if hasattr(obj, "org"):
                return obj.org
        pk = self.kwargs.get(self.organization_lookup_kwarg)
        if pk is None:
            session_org_id = self.request.session.get("active_org_id")
            if session_org_id:
                try:
                    return Organization.objects.get(pk=session_org_id)
                except Organization.DoesNotExist:  # pragma: no cover
                    pass
            active = getattr(self.request, "active_org", None)
            if active:
                return active
            return getattr(self.request.user, "current_org", None)
        return get_object_or_404(Organization, pk=pk)


class OrganizationAdminRequiredMixin(OrganizationPermissionRequiredMixin):
    """Convenience subclass: require ``ADMIN_MANAGE_ORG`` on the target org.

    Preserves the original mixin's behaviour for the broad "must be an org
    admin" use case. New views that need a narrower permission (e.g.
    ``GUEST_INVITE``) should subclass
    :class:`OrganizationPermissionRequiredMixin` directly so the gate
    matches their actual authorization need.
    """

    required_org_permission = PermissionCode.ADMIN_MANAGE_ORG


class SuperuserRequiredMixin(AccessMixin):
    """Verify that the current user is logged in and is a superuser."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            # Redirect to login if not logged in
            return self.handle_no_permission()

        if not request.user.is_superuser:
            raise PermissionDenied("You do not have access to this page.")

        return super().dispatch(request, *args, **kwargs)
