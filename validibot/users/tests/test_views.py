"""Tests for user-facing account views.

These tests cover the profile views plus the personal API-key page. The
API-key cases are security-sensitive because the UI must only show newly
issued bearer secrets once and must never read them back from storage.
"""

from http import HTTPStatus

import pytest
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import AnonymousUser
from django.contrib.messages.middleware import MessageMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.http import HttpRequest
from django.http import HttpResponseRedirect
from django.test import RequestFactory
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from validibot.users.forms import UserAdminChangeForm
from validibot.users.models import User
from validibot.users.models import ValidibotAPIKey
from validibot.users.services.api_keys import issue_api_key
from validibot.users.tests.factories import UserFactory
from validibot.users.views import UserRedirectView
from validibot.users.views import UserUpdateView
from validibot.users.views import user_detail_view

pytestmark = pytest.mark.django_db


class TestUserUpdateView:
    """Tests for the user profile update view."""

    def dummy_get_response(self, request: HttpRequest):
        return None

    def test_get_success_url(self, user: User, rf: RequestFactory):
        view = UserUpdateView()
        request = rf.get("/fake-url/")
        request.user = user

        view.request = request
        assert view.get_success_url() == f"/app/users/{user.username}/"

    def test_get_object(self, user: User, rf: RequestFactory):
        view = UserUpdateView()
        request = rf.get("/fake-url/")
        request.user = user

        view.request = request

        assert view.get_object() == user

    def test_form_valid(self, user: User, rf: RequestFactory):
        view = UserUpdateView()
        request = rf.get("/fake-url/")

        # Add the session/message middleware to the request
        SessionMiddleware(self.dummy_get_response).process_request(request)
        MessageMiddleware(self.dummy_get_response).process_request(request)
        request.user = user

        view.request = request

        # Initialize the form
        form = UserAdminChangeForm()
        form.cleaned_data = {}
        form.instance = user
        view.form_valid(form)

        messages_sent = [m.message for m in messages.get_messages(request)]
        assert messages_sent == [_("Profile updated successfully")]


class TestUserRedirectView:
    def test_get_redirect_url(self, user: User, rf: RequestFactory):
        view = UserRedirectView()
        request = rf.get("/fake-url")
        request.user = user

        view.request = request
        assert view.get_redirect_url() == f"/app/users/{user.username}/"


class TestUserDetailView:
    def test_authenticated(self, user: User, rf: RequestFactory):
        request = rf.get("/fake-url/")
        request.user = UserFactory()
        response = user_detail_view(request, username=user.username)

        assert response.status_code == HTTPStatus.OK

    def test_not_authenticated(self, user: User, rf: RequestFactory):
        request = rf.get("/fake-url/")
        request.user = AnonymousUser()
        response = user_detail_view(request, username=user.username)
        login_url = reverse(settings.LOGIN_URL)

        assert isinstance(response, HttpResponseRedirect)
        assert response.status_code == HTTPStatus.FOUND
        assert response.url == f"{login_url}?next=/fake-url/"


class TestUserApiKeyRotateView:
    """Rotation view tests lock in one-time-display API-key behavior."""

    def test_rotates_key_with_htmx(self, client, user):
        """HTMX rotation returns the only copyable plaintext key."""

        client.force_login(user)
        original = issue_api_key(user=user).api_key

        response = client.post(
            reverse("users:api-key-rotate"),
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == HTTPStatus.OK
        assert response.headers.get("HX-Trigger") == "apiKeyRotated"
        original.refresh_from_db()
        new_key = ValidibotAPIKey.objects.get(user=user, revoked_at__isnull=True)
        content = response.content.decode()
        assert original.revoked_at is not None
        assert new_key.public_id in content
        assert "vbk_1_" in content
        assert new_key.secret_digest not in content
        assert "Copy this key now" in content

    def test_rotates_key_without_htmx(self, client, user):
        """Plain POST renders the one-time key instead of redirect-losing it."""

        client.force_login(user)
        original = issue_api_key(user=user).api_key

        response = client.post(reverse("users:api-key-rotate"))

        assert response.status_code == HTTPStatus.OK
        original.refresh_from_db()
        new_key = ValidibotAPIKey.objects.get(user=user, revoked_at__isnull=True)
        content = response.content.decode()
        assert original.revoked_at is not None
        assert new_key.public_id in content
        assert "vbk_1_" in content


class TestUserApiKeyView:
    """API-key page tests ensure saved secrets are not re-displayed."""

    def test_get_does_not_create_or_display_secret(self, client, user):
        """Loading the page must not mint a bearer secret as a side effect."""

        client.force_login(user)
        response = client.get(reverse("users:api-key"))

        assert response.status_code == HTTPStatus.OK
        assert response.context_data["api_key"] is None
        assert ValidibotAPIKey.objects.filter(user=user).count() == 0
        content = response.content.decode()
        assert "No active API key" in content
        assert 'data-copy-target="#api-key-value"' not in content

    def test_get_shows_only_redacted_existing_key(self, client, user):
        """Saved API keys are visible only as redacted identifiers."""

        issued = issue_api_key(user=user)
        client.force_login(user)

        response = client.get(reverse("users:api-key"))

        assert response.status_code == HTTPStatus.OK
        assert response.context_data["api_key"] == issued.api_key
        content = response.content.decode()
        assert issued.api_key.redacted_key in content
        assert issued.full_key not in content
        assert issued.api_key.secret_digest not in content
        assert 'data-copy-target="#api-key-value"' not in content

    def test_api_key_panel_contains_copy_script(self, client, user):
        """The copy handler exists, but copy controls appear only on issuance."""

        client.force_login(user)

        response = client.get(reverse("users:api-key"))
        content = response.content.decode()
        assert "__svCopyHandlerBound" in content
        assert 'data-copy-target="#api-key-value"' not in content

        hx_response = client.post(
            reverse("users:api-key-rotate"),
            HTTP_HX_REQUEST="true",
        )
        hx_content = hx_response.content.decode()
        assert "__svCopyHandlerBound" in hx_content
        assert 'data-copy-target="#api-key-value"' in hx_content
