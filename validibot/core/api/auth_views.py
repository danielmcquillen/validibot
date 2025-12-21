"""
Authentication API endpoints.

Provides a minimal endpoint for token verification and user identification,
following the GitHub API pattern (GET /user returns authenticated user info).
"""

from drf_spectacular.utils import extend_schema
from drf_spectacular.utils import inline_serializer
from rest_framework import serializers
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView


class AuthMeView(APIView):
    """
    Get the currently authenticated user's basic information.

    This endpoint serves two purposes:
    1. Token validation - returns 401/403 if token is invalid
    2. User identification - returns email/name for display

    Used by the CLI during login to verify tokens and show user info.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Get current user info",
        description=(
            "Returns the email and name of the currently authenticated user. "
            "This endpoint is used to validate API tokens and retrieve basic "
            "user information for display purposes."
        ),
        responses={
            200: inline_serializer(
                name="AuthMeResponse",
                fields={
                    "email": serializers.EmailField(
                        help_text="The user's email address",
                    ),
                    "name": serializers.CharField(
                        help_text="The user's display name (may be empty)",
                    ),
                },
            ),
            401: {"description": "Authentication credentials were not provided."},
            403: {"description": "Invalid or expired token."},
        },
        tags=["Authentication"],
    )
    def get(self, request):
        """Return the authenticated user's email and name."""
        user = request.user
        return Response(
            {
                "email": user.email,
                "name": user.name or "",
            },
            status=status.HTTP_200_OK,
        )
