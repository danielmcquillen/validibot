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
from rest_framework.authtoken.models import Token

from validibot.users.forms import UserAdminChangeForm
from validibot.users.models import User
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
    def test_rotates_token_with_htmx(self, client, user):
        client.force_login(user)
        original_token, _ = Token.objects.get_or_create(user=user)

        response = client.post(
            reverse("users:api-key-rotate"),
            HTTP_HX_REQUEST="true",
        )

        assert response.status_code == HTTPStatus.OK
        assert response.headers.get("HX-Trigger") == "apiKeyRotated"
        new_token = Token.objects.get(user=user)
        assert new_token.key != original_token.key

    def test_rotates_token_without_htmx(self, client, user):
        client.force_login(user)
        original_token, _ = Token.objects.get_or_create(user=user)

        response = client.post(reverse("users:api-key-rotate"))

        assert response.status_code == HTTPStatus.FOUND
        assert response.url == reverse("users:api-key")
        new_token = Token.objects.get(user=user)
        assert new_token.key != original_token.key


class TestUserApiKeyView:
    def test_api_key_context_contains_token(self, client, user):
        client.force_login(user)
        response = client.get(reverse("users:api-key"))

        assert response.status_code == HTTPStatus.OK
        assert response.context_data["api_token"].user == user

    def test_api_key_panel_contains_copy_script(self, client, user):
        client.force_login(user)

        response = client.get(reverse("users:api-key"))
        content = response.content.decode()
        assert "__svCopyHandlerBound" in content
        assert 'data-copy-target="#api-key-value"' in content

        hx_response = client.post(
            reverse("users:api-key-rotate"),
            HTTP_HX_REQUEST="true",
        )
        hx_content = hx_response.content.decode()
        assert "__svCopyHandlerBound" in hx_content
