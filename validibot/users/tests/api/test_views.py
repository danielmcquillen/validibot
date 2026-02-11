import pytest
from rest_framework.test import APIRequestFactory

from validibot.users.api.views import UserViewSet
from validibot.users.models import User


class TestUserViewSet:
    """
    Tests for the UserViewSet API.

    The UserViewSet only exposes the 'me' action to return the current user.
    List, retrieve, and update operations are not available via API.
    See ADR-2025-12-22 for rationale on API restrictions.
    """

    @pytest.fixture
    def api_rf(self) -> APIRequestFactory:
        return APIRequestFactory()

    def test_me(self, user: User, api_rf: APIRequestFactory):
        """Test that the me action returns the current user's details."""
        view = UserViewSet()
        request = api_rf.get("/fake-url/")
        request.user = user

        view.request = request

        response = view.me(request)  # type: ignore[call-arg, arg-type, misc]

        assert response.data == {
            "username": user.username,
            "name": user.name,
        }
