"""
Tests for User API URL routing.

The UserViewSet only exposes the 'me' action. List and detail views
are not available via API. See ADR-2025-12-22 for rationale.
"""

import pytest
from django.urls import NoReverseMatch
from django.urls import resolve
from django.urls import reverse


def test_user_me():
    """Test that the /users/me/ route is available."""
    assert reverse("api:user-me") == "/api/v1/users/me/"
    assert resolve("/api/v1/users/me/").view_name == "api:user-me"


def test_user_list_not_available():
    """Test that the /users/ list route is not available."""
    # The user-list route should not exist since we removed ListModelMixin
    with pytest.raises(NoReverseMatch):
        reverse("api:user-list")


def test_user_detail_not_available():
    """Test that the /users/<username>/ detail route is not available."""
    # The user-detail route should not exist since we removed RetrieveModelMixin
    with pytest.raises(NoReverseMatch):
        reverse("api:user-detail", kwargs={"username": "testuser"})
