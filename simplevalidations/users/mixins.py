from __future__ import annotations

from django.conf import settings
from django.contrib.auth.mixins import AccessMixin, LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect

from simplevalidations.users.models import Organization
from simplevalidations.users.permissions import PermissionCode
from simplevalidations.users.scoping import ensure_active_org_scope


class OrganizationAdminRequiredMixin(LoginRequiredMixin):
    """Mixin ensuring the user has admin rights on the target organization."""

    organization_lookup_kwarg = "pk"
    organization_context_attr = "managed_organization"

    def dispatch(self, request, *args, **kwargs):
        memberships, _, _ = ensure_active_org_scope(request)
        organization = self.get_organization()
        if organization is None:
            raise PermissionDenied("Organization context is required.")

        membership = next(
            (m for m in memberships if m.org_id == organization.id),
            None,
        )
        if not membership or not request.user.has_perm(
            PermissionCode.ADMIN_MANAGE_ORG.value,
            organization,
        ):
            raise PermissionDenied(
                "You do not have administrator access to this organization."
            )

        setattr(self, self.organization_context_attr, organization)
        self.organization_membership = membership
        request.active_org = organization
        request.session["active_org_id"] = organization.id

        return super().dispatch(request, *args, **kwargs)

    def get_organization(self) -> Organization | None:
        if hasattr(self, "object") and getattr(self, "object", None):
            obj = getattr(self, "object")
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


class SuperuserRequiredMixin(AccessMixin):
    """Verify that the current user is logged in and is a superuser."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            # Redirect to login if not logged in
            return self.handle_no_permission()

        if not request.user.is_superuser:
            raise PermissionDenied("You do not have access to this page.")

        return super().dispatch(request, *args, **kwargs)
