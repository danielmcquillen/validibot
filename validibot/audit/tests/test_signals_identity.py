"""Integration tests for the identity / session audit capture points.

Wave-2 added a slice of account-security signal receivers on top of the
original login / password-change / token captures. Each one translates an
allauth (or ``allauth.mfa``) signal into a single ``AuditLogEntry`` so an
operator or auditor can answer *"who changed their security posture, when,
and from where?"*:

* ``SESSION_REVOKED`` — allauth's ``user_logged_out`` (the inverse of the
  existing LOGIN_SUCCEEDED capture).
* ``PASSWORD_RESET_REQUESTED`` — allauth's ``password_reset`` (fired on
  reset *completion*; we tag ``metadata.phase`` so it isn't confused with
  an interactive ``PASSWORD_CHANGED``).
* ``MFA_ENABLED`` / ``MFA_DISABLED`` / ``MFA_CHALLENGE_FAILED`` — the
  ``allauth.mfa`` authenticator-lifecycle signals. Only the *type* of
  factor is recorded, never the secret material.
* ``EMAIL_ADDED`` / ``EMAIL_CHANGED`` / ``EMAIL_VERIFIED`` /
  ``EMAIL_REMOVED`` — the allauth email-management signals. These record
  the *fact* of the change only: an address is PII and must never reach
  ``changes``, ``metadata`` or ``target_repr``. ``test_email_value_never_
  leaks`` is the guard for that contract — the negative test the audit
  design doc mandates for every PII-adjacent capture point.

The receivers read actor/request context from ``get_current_context()``;
firing the signal directly (as these tests do) exercises the empty-context
fallback, which is the realistic shape for signals that originate outside
an HTTP request.
"""

from __future__ import annotations

from types import SimpleNamespace

from allauth.account.signals import email_added
from allauth.account.signals import email_changed
from allauth.account.signals import email_confirmed
from allauth.account.signals import email_removed
from allauth.account.signals import password_reset
from allauth.account.signals import user_logged_out
from allauth.mfa.signals import authentication_failed
from allauth.mfa.signals import authenticator_added
from allauth.mfa.signals import authenticator_removed
from django.test import RequestFactory
from django.test import TestCase

from validibot.audit.constants import AuditAction
from validibot.audit.models import AuditLogEntry
from validibot.users.tests.factories import UserFactory


class SessionAndMFASignalTests(TestCase):
    """Logout, password-reset and MFA-lifecycle capture paths.

    These are the non-PII account-security events: each test asserts the
    right action lands, attributed to the acting user, exactly once.
    """

    def setUp(self) -> None:
        """Start each test from an empty audit table.

        Other apps create fixtures at import time; clearing here keeps the
        ``filter(action=...)`` assertions exact rather than ">= 1".
        """

        self.factory = RequestFactory()
        AuditLogEntry.objects.all().delete()

    def test_logout_records_session_revoked(self) -> None:
        """``user_logged_out`` is the inverse of login and must leave a
        SESSION_REVOKED trail so a sign-out shows up in incident review.
        """

        user = UserFactory()
        request = self.factory.get("/accounts/logout/")
        user_logged_out.send(sender=user.__class__, request=request, user=user)

        entry = AuditLogEntry.objects.get(action=AuditAction.SESSION_REVOKED.value)
        self.assertEqual(entry.actor.user, user)

    def test_password_reset_records_request_with_phase(self) -> None:
        """allauth fires ``password_reset`` on reset *completion*; we file
        it under PASSWORD_RESET_REQUESTED and tag the phase so the entry is
        unambiguous to a reader who only sees the action code.
        """

        user = UserFactory()
        request = self.factory.get("/accounts/password/reset/key/done/")
        password_reset.send(sender=user.__class__, request=request, user=user)

        entry = AuditLogEntry.objects.get(
            action=AuditAction.PASSWORD_RESET_REQUESTED.value,
        )
        self.assertEqual(entry.actor.user, user)
        self.assertEqual(entry.metadata["phase"], "completed")

    def test_mfa_enabled_records_factor_type_only(self) -> None:
        """Adding an authenticator is a high-value security event. We
        record MFA_ENABLED with the *kind* of factor (for incident triage)
        but never the secret material itself.
        """

        user = UserFactory()
        request = self.factory.get("/account/2fa/totp/activate/")
        # ``data`` is where allauth stores the cleartext factor secret;
        # the receiver must read only ``type``, never ``data``.
        authenticator = SimpleNamespace(type="totp", data="TOPSECRET")
        authenticator_added.send(
            sender=user.__class__,
            request=request,
            user=user,
            authenticator=authenticator,
        )

        entry = AuditLogEntry.objects.get(action=AuditAction.MFA_ENABLED.value)
        self.assertEqual(entry.metadata["authenticator_type"], "totp")
        # The secret must never be captured anywhere on the entry.
        self.assertNotIn("TOPSECRET", f"{entry.metadata}{entry.changes}")

    def test_mfa_disabled_records_entry(self) -> None:
        """Removing an authenticator weakens the account and is exactly the
        kind of change a compromised-account investigation looks for.
        """

        user = UserFactory()
        request = self.factory.get("/account/2fa/totp/deactivate/")
        authenticator_removed.send(
            sender=user.__class__,
            request=request,
            user=user,
            authenticator=SimpleNamespace(type="totp"),
        )

        self.assertTrue(
            AuditLogEntry.objects.filter(
                action=AuditAction.MFA_DISABLED.value,
                actor__user=user,
            ).exists(),
        )

    def test_mfa_challenge_failed_records_entry(self) -> None:
        """A failed MFA challenge is a brute-force / phishing signal worth a
        dedicated entry so log-based alerts can fire on bursts.
        """

        user = UserFactory()
        request = self.factory.get("/account/2fa/authenticate/")
        authentication_failed.send(
            sender=user.__class__,
            request=request,
            user=user,
            authenticator=SimpleNamespace(type="totp"),
        )

        self.assertTrue(
            AuditLogEntry.objects.filter(
                action=AuditAction.MFA_CHALLENGE_FAILED.value,
            ).exists(),
        )


class EmailLifecycleSignalTests(TestCase):
    """Email add / change / verify / remove capture paths.

    The defining constraint here is privacy: an email address is PII, so
    these events record only the *fact* of the change. The address value
    must never be written to the immutable entry.
    """

    NEW_EMAIL = "alias@private.example"
    OLD_EMAIL = "old@private.example"

    def setUp(self) -> None:
        """Clean audit table per test (see SessionAndMFASignalTests)."""

        self.factory = RequestFactory()
        AuditLogEntry.objects.all().delete()

    def test_email_added_records_fact(self) -> None:
        """Adding an email is recorded as EMAIL_ADDED against the user."""

        user = UserFactory()
        email_added.send(
            sender=user.__class__,
            request=self.factory.get("/account/email/"),
            user=user,
            email_address=SimpleNamespace(user=user, email=self.NEW_EMAIL),
        )

        self.assertTrue(
            AuditLogEntry.objects.filter(
                action=AuditAction.EMAIL_ADDED.value,
                actor__user=user,
            ).exists(),
        )

    def test_email_changed_and_verified_record_facts(self) -> None:
        """Primary-email change and verification each leave their own entry
        so the sequence (changed → verified) is auditable. ``email_confirmed``
        carries no ``user`` kwarg, so this also exercises the resolve-from-
        ``email_address.user`` path.
        """

        user = UserFactory()
        request = self.factory.get("/account/email/")
        addr = SimpleNamespace(user=user, email=self.NEW_EMAIL)

        email_changed.send(
            sender=user.__class__,
            request=request,
            user=user,
            from_email_address=SimpleNamespace(user=user, email=self.OLD_EMAIL),
            to_email_address=addr,
        )
        email_confirmed.send(
            sender=user.__class__,
            request=request,
            email_address=addr,
        )

        self.assertTrue(
            AuditLogEntry.objects.filter(
                action=AuditAction.EMAIL_CHANGED.value,
                actor__user=user,
            ).exists(),
        )
        self.assertTrue(
            AuditLogEntry.objects.filter(
                action=AuditAction.EMAIL_VERIFIED.value,
                actor__user=user,
            ).exists(),
        )

    def test_email_removed_records_fact(self) -> None:
        """Removing an email is recorded as EMAIL_REMOVED."""

        user = UserFactory()
        email_removed.send(
            sender=user.__class__,
            request=self.factory.get("/account/email/"),
            user=user,
            email_address=SimpleNamespace(user=user, email=self.NEW_EMAIL),
        )

        self.assertTrue(
            AuditLogEntry.objects.filter(
                action=AuditAction.EMAIL_REMOVED.value,
                actor__user=user,
            ).exists(),
        )

    def test_email_value_never_leaks(self) -> None:
        """**Security contract.** The email-address *value* is PII and must
        never appear in ``changes``, ``metadata`` or ``target_repr`` on any
        email-lifecycle entry — only the fact of the change, attributed to
        the user via the (erasable) actor layer. This is the negative test
        the audit design doc mandates for PII-adjacent capture points.
        """

        user = UserFactory()
        request = self.factory.get("/account/email/")
        email_changed.send(
            sender=user.__class__,
            request=request,
            user=user,
            from_email_address=SimpleNamespace(user=user, email=self.OLD_EMAIL),
            to_email_address=SimpleNamespace(user=user, email=self.NEW_EMAIL),
        )

        email_actions = [
            AuditAction.EMAIL_ADDED.value,
            AuditAction.EMAIL_CHANGED.value,
            AuditAction.EMAIL_VERIFIED.value,
            AuditAction.EMAIL_REMOVED.value,
        ]
        entries = AuditLogEntry.objects.filter(action__in=email_actions)
        self.assertTrue(entries.exists())
        for entry in entries:
            blob = f"{entry.changes}{entry.metadata}{entry.target_repr}"
            self.assertNotIn(self.NEW_EMAIL, blob)
            self.assertNotIn(self.OLD_EMAIL, blob)
            # Belt and braces: no "@" at all in the non-actor fields.
            self.assertNotIn("@", blob)
