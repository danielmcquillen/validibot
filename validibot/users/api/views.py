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

    Currently only provides the `/users/me/` endpoint to retrieve information
    about the authenticated user.
    """

    serializer_class = UserSerializer
    queryset = User.objects.all()

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
