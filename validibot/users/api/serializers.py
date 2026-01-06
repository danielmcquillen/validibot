from rest_framework import serializers

from validibot.users.models import Organization
from validibot.users.models import User


class UserSerializer(serializers.ModelSerializer[User]):
    """
    Serializer for the User model.

    Only exposes basic user information (username, name) since the API
    is minimal and doesn't include user detail views. See ADR-2025-12-22.
    """

    class Meta:
        model = User
        fields = ["username", "name"]


class OrganizationSerializer(serializers.ModelSerializer[Organization]):
    """
    Read-only serializer for organizations the user has access to.

    Exposes only basic organization info needed for API clients to
    identify and select an organization context.
    """

    class Meta:
        model = Organization
        fields = ["slug", "name"]
        read_only_fields = fields
