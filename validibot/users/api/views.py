from __future__ import annotations

from typing import TYPE_CHECKING

from rest_framework import permissions
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet
from rest_framework.viewsets import ReadOnlyModelViewSet

from validibot.users.models import Organization
from validibot.users.models import User
from validibot.workflows.models import WorkflowAccessGrant

from .serializers import OrganizationSerializer
from .serializers import UserSerializer

if TYPE_CHECKING:
    from django.db.models import QuerySet


class UserViewSet(GenericViewSet):
    """
    User information endpoints.

    Currently only provides the ``/users/me/`` endpoint to retrieve information
    about the authenticated user. ``me`` is *self-scoped* — a caller can only
    ever read their own profile — and is intentionally the sole user-facing API
    route. There is deliberately no user-management API; creating, updating, and
    deleting users happens through the Django admin and the org-scoped
    membership views, not here.
    """

    serializer_class = UserSerializer
    # SECURITY: keep this a bare ``GenericViewSet`` exposing only the ``me``
    # action. Do NOT add ``ListModelMixin`` / ``RetrieveModelMixin`` or switch
    # the base to ``ModelViewSet`` — combined with the ``User.objects.all()``
    # queryset below, the router would immediately expose ``GET /users/`` (and
    # ``/users/<pk>/``) returning every user in every org to any authenticated
    # caller: a cross-tenant enumeration leak. The guard tests in
    # ``users/tests/api/test_urls.py`` assert those routes stay absent.
    queryset = User.objects.all()
    # Declare auth and the read-only verb set explicitly instead of leaning on
    # the global ``DEFAULT_PERMISSION_CLASSES`` / method defaults, so a change to
    # those project-wide settings (e.g. flipping ``DRF_ALLOW_ANONYMOUS``) can't
    # silently open this endpoint. Mirrors ``OrganizationViewSet`` below.
    permission_classes = [permissions.IsAuthenticated]
    http_method_names = ["get", "head", "options"]

    @action(detail=False, methods=["get"])
    def me(self, request):
        """
        Get the authenticated user's profile.

        Returns the username and display name of the currently authenticated user.
        """
        serializer = UserSerializer(request.user, context={"request": request})
        return Response(status=status.HTTP_200_OK, data=serializer.data)


class OrganizationViewSet(ReadOnlyModelViewSet):
    """
    List organizations you have access to.

    Returns all organizations where you are a member or have been granted
    guest access to specific workflows. Use the organization `slug` in
    other API endpoints (e.g., `/orgs/{slug}/workflows/`).
    """

    serializer_class = OrganizationSerializer
    permission_classes = [permissions.IsAuthenticated]
    # Disable detail view - users should use org-scoped endpoints directly
    http_method_names = ["get", "head", "options"]
    lookup_field = "slug"

    def get_queryset(self) -> QuerySet[Organization]:
        """
        Return organizations accessible to the authenticated user.

        A user has access to an organization if:
        1. They have an active membership in the organization, OR
        2. They have an active WorkflowAccessGrant for any workflow in the org
        """
        # drf-spectacular introspects the view with no real request; an
        # empty queryset lets it derive the model and path-parameter
        # types without touching request.user.
        if getattr(self, "swagger_fake_view", False):
            return Organization.objects.none()

        user = self.request.user

        # Orgs via active membership
        membership_org_ids = user.memberships.filter(
            is_active=True,
        ).values_list("org_id", flat=True)

        # Orgs via workflow guest grants
        grant_org_ids = WorkflowAccessGrant.objects.filter(
            user=user,
            is_active=True,
        ).values_list("workflow__org_id", flat=True)

        # Combine and deduplicate
        all_org_ids = set(membership_org_ids) | set(grant_org_ids)

        return Organization.objects.filter(pk__in=all_org_ids).order_by("name")
