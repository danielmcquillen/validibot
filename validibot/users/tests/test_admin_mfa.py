"""Tests for mandatory MFA assurance at the Django admin boundary.

These tests cover the security distinction between merely routing admin login
through django-allauth and actually requiring every privileged session to use
MFA. The gate must reject unenrolled staff, challenge enrolled staff whose
current session is password-only, accept a live MFA session, and immediately
reject that session if its authenticator is removed.
"""

from __future__ import annotations

import time
from http import HTTPStatus
from urllib.parse import urlencode

from allauth.mfa.models import Authenticator
from django.contrib.auth import REDIRECT_FIELD_NAME
from django.test import TestCase
from django.test import override_settings
from django.urls import reverse

from validibot.users.models import User


@override_settings(DJANGO_ADMIN_REQUIRE_MFA=True)
class AdminMFAEnforcementTests(TestCase):
    """Exercise enrolment and session assurance for a privileged user."""

    def setUp(self) -> None:
        """Create and authenticate one superuser for each isolated test."""
        self.user = User.objects.create_superuser(
            username="mfa-admin",
            email="mfa-admin@example.com",
            password="test-admin-password",  # noqa: S106
        )
        self.client.force_login(self.user)
        self.admin_url = reverse("admin:index")

    def _enrol_totp(self) -> Authenticator:
        """Create the row needed for an enrolled primary TOTP factor."""
        return Authenticator.objects.create(
            user=self.user,
            type=Authenticator.Type.TOTP,
            data={"secret": "placeholder-not-decrypted"},
        )

    def _record_session_mfa(self, authenticator: Authenticator) -> None:
        """Record the same session evidence allauth writes after MFA use."""
        session = self.client.session
        session["account_authentication_methods"] = [
            {
                "method": "mfa",
                "at": time.time(),
                "id": authenticator.pk,
                "type": authenticator.type,
            },
        ]
        session.save()

    def test_unenrolled_staff_is_sent_to_mfa_setup(self) -> None:
        """A password alone must not let a newly promoted administrator in."""
        response = self.client.get(self.admin_url)

        expected_query = urlencode({REDIRECT_FIELD_NAME: self.admin_url})
        expected_url = f"{reverse('mfa_activate_totp')}?{expected_query}"
        self.assertRedirects(response, expected_url, fetch_redirect_response=False)

    def test_enrolled_staff_with_password_only_session_is_challenged(self) -> None:
        """Enrolment alone is insufficient when this session never used MFA."""
        self._enrol_totp()

        response = self.client.get(self.admin_url)

        expected_query = urlencode({REDIRECT_FIELD_NAME: self.admin_url})
        expected_url = f"{reverse('mfa_reauthenticate')}?{expected_query}"
        self.assertRedirects(response, expected_url, fetch_redirect_response=False)

    def test_current_mfa_session_can_access_admin(self) -> None:
        """A session proven with a still-enrolled authenticator may enter."""
        authenticator = self._enrol_totp()
        self._record_session_mfa(authenticator)

        response = self.client.get(self.admin_url)

        self.assertEqual(response.status_code, HTTPStatus.OK)

    def test_deleted_authenticator_invalidates_admin_assurance(self) -> None:
        """Removing the factor must invalidate its old session evidence."""
        authenticator = self._enrol_totp()
        self._record_session_mfa(authenticator)
        authenticator.delete()

        response = self.client.get(self.admin_url)

        expected_query = urlencode({REDIRECT_FIELD_NAME: self.admin_url})
        expected_url = f"{reverse('mfa_activate_totp')}?{expected_query}"
        self.assertRedirects(response, expected_url, fetch_redirect_response=False)

    def test_gate_can_be_disabled_for_documented_break_glass(self) -> None:
        """Operators need an explicit recovery path if allauth MFA is broken."""
        with override_settings(DJANGO_ADMIN_REQUIRE_MFA=False):
            response = self.client.get(self.admin_url)

        self.assertEqual(response.status_code, HTTPStatus.OK)
