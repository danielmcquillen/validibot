"""Tests for login form error display.

The login form renders via ``account/partial/login_form.html`` inside
allauth's ``account/login.html``. Allauth handles the authentication
logic; these tests verify that our Validibot-branded template correctly
surfaces error messages when authentication fails — something allauth's
own test suite doesn't cover because it doesn't know about our custom
templates.

These tests exist because a production outage was caused by the login
form silently swallowing errors: the form POST returned 200 (re-rendered
with validation errors), but no error text appeared on the page because
the template was missing a ``form.non_field_errors`` block. The user
saw the page refresh with no explanation.

We deliberately don't re-test allauth's authentication logic (password
hashing, account locking, email verification). We test the Validibot
template wiring: "when allauth returns errors, does the user see them?"
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from allauth.account.models import EmailAddress
from django.urls import reverse

from validibot.users.models import User

pytestmark = pytest.mark.django_db


class TestLoginFormErrorDisplay:
    """Login form must show visible error messages on authentication failure."""

    @pytest.fixture
    def login_user(self):
        """Create a user with a known password and verified email.

        allauth requires ``ACCOUNT_EMAIL_VERIFICATION = "mandatory"`` in
        our settings, so a user created with ``create_user()`` alone
        can't log in — allauth rejects them with an "unverified email"
        error. We mark the address as verified up front so tests
        exercise the credential-check path, not the email-verification
        path.
        """
        user = User.objects.create_user(
            username="logintest",
            email="logintest@example.com",
            password="CorrectHorse42!",  # noqa: S106
        )
        EmailAddress.objects.create(
            user=user,
            email=user.email,
            verified=True,
            primary=True,
        )
        return user

    def test_wrong_password_shows_error_message(self, client, login_user):
        """A failed login must render an error message the user can read.

        If this fails, the login form silently refreshes on bad
        credentials — the exact production bug this test was written
        to prevent. allauth puts the "email/password not correct"
        message into ``form.non_field_errors``, so the template must
        render that block.
        """
        response = client.post(
            reverse("account_login"),
            {
                "login": login_user.email,
                "password": "WrongPassword99!",
            },
        )
        # allauth re-renders the form with errors (200), not redirect (302).
        assert response.status_code == HTTPStatus.OK
        body = response.content.decode()
        # allauth's error message for bad credentials — the exact wording
        # may vary by allauth version, but it always contains one of these.
        assert any(
            phrase in body
            for phrase in [
                "not correct",
                "not valid",
                "unable to log in",
                "email address and/or password",
            ]
        ), (
            "Login form must display an error message for wrong credentials. "
            "If this fails, the template is missing the non_field_errors block."
        )

    def test_empty_form_shows_field_errors(self, client):
        """Submitting an empty form must show per-field required errors.

        Crispy Forms renders these via ``as_crispy_field``, so this test
        confirms the field-level error path works alongside the
        non-field-error path tested above.
        """
        response = client.post(
            reverse("account_login"),
            {},
        )
        assert response.status_code == HTTPStatus.OK
        body = response.content.decode()
        # Django/allauth marks empty required fields with an error.
        assert "required" in body.lower() or "this field" in body.lower(), (
            "Empty login form must show 'required' field errors."
        )

    def test_successful_login_with_username_redirects(self, client, login_user):
        """Login with username + correct password must redirect (302).

        ``ACCOUNT_LOGIN_METHODS`` includes ``"username"``, so this
        must work. If it returns 200, the auth pipeline is broken.
        """
        response = client.post(
            reverse("account_login"),
            {
                "login": login_user.username,
                "password": "CorrectHorse42!",
            },
        )
        assert response.status_code == HTTPStatus.FOUND

    def test_successful_login_with_email_redirects(self, client, login_user):
        """Login with email + correct password must also redirect (302).

        ``ACCOUNT_LOGIN_METHODS`` includes ``"email"``, so users can
        sign in with their email address instead of remembering their
        username. This was added because the sign-up form prominently
        collects email, so users naturally try it at login.
        """
        response = client.post(
            reverse("account_login"),
            {
                "login": login_user.email,
                "password": "CorrectHorse42!",
            },
        )
        assert response.status_code == HTTPStatus.FOUND
