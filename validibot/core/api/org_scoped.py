"""
Mixin and permission classes for org-scoped API viewsets.

This module provides automatic org resolution from URL kwargs and enforces
org membership for API endpoints (ADR-2026-01-06).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.shortcuts import get_object_or_404
from rest_framework import permissions

if TYPE_CHECKING:
    from rest_framework.request import Request
    from rest_framework.views import APIView

    from validibot.users.models import Membership
    from validibot.users.models import Organization


class OrgScopedMixin:
    """
    Mixin that resolves org from URL path and enforces membership.

    Expects URL pattern to include `org_slug` kwarg:
        path("orgs/<slug:org_slug>/workflows/", ...)

    Sets self.org on first access and provides get_org() and get_membership()
    helpers for viewsets.

    Usage:
        class MyViewSet(OrgScopedMixin, viewsets.ModelViewSet):
            def get_queryset(self):
                return MyModel.objects.filter(org=self.get_org())
    """

    _org: Organization | None = None
    _membership: Membership | None = None

    def get_org(self) -> Organization:
        """
        Return the organization from the URL path.

        Raises Http404 if the org doesn't exist.
        """
        if self._org is None:
            from validibot.users.models import Organization

            org_slug = self.kwargs.get("org_slug")
            self._org = get_object_or_404(Organization, slug=org_slug)
        return self._org

    def get_membership(self) -> Membership | None:
        """
        Return the user's active membership in the org, or None.

        Returns None if the user is not authenticated or not a member.
        """
        if self._membership is None:
            from validibot.users.models import Membership

            org = self.get_org()
            user = self.request.user
            if user.is_authenticated:
                self._membership = Membership.objects.filter(
                    user=user,
                    org=org,
                    is_active=True,
                ).first()
        return self._membership

    @property
    def org(self) -> Organization:
        """Convenience property to access the org."""
        return self.get_org()


class OrgMembershipPermission(permissions.BasePermission):
    """
    Permission class that checks user has access to the org in the URL.

    Access is granted if the user:
    - Is a superuser
    - Is a member of the org
    - Has a workflow access grant for any workflow in the org (guest access)

    Requires the view to use OrgScopedMixin or provide get_org() and
    get_membership() methods.
    """

    message = "You must be a member of this organization or have a workflow grant."

    def has_permission(self, request: Request, view: APIView) -> bool:
        # Superusers always have access
        if request.user.is_authenticated and request.user.is_superuser:
            return True

        # Check if view has org-scoping capability
        if not hasattr(view, "get_membership") or not hasattr(view, "get_org"):
            return True

        # Check org membership first (most common case)
        membership = view.get_membership()
        if membership is not None:
            return True

        # Check for guest grants (workflow access grants in this org)
        if request.user.is_authenticated:
            from validibot.workflows.models import WorkflowAccessGrant

            org = view.get_org()
            has_grant = WorkflowAccessGrant.objects.filter(
                user=request.user,
                workflow__org=org,
                is_active=True,
            ).exists()
            if has_grant:
                return True

        return False
