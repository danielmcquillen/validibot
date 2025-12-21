from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from validibot.users.models import User

from .serializers import UserSerializer


class UserViewSet(GenericViewSet):
    """
    Minimal user API for CLI and programmatic access.

    Only exposes the 'me' action to retrieve the current user's information.
    List, retrieve, and update operations are not available via API - users
    must use the web interface for profile management.

    This restriction is intentional to minimize the API attack surface
    during the initial CLI rollout. See ADR-2025-12-22 for rationale.
    """

    serializer_class = UserSerializer
    queryset = User.objects.all()

    @action(detail=False, methods=["get"])
    def me(self, request):
        """Return the current authenticated user's information."""
        serializer = UserSerializer(request.user, context={"request": request})
        return Response(status=status.HTTP_200_OK, data=serializer.data)
