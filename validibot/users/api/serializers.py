from rest_framework import serializers

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
