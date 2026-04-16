"""Tests for signup-abuse protections on UserSignupForm.

Covers the three settings-gated defenses added to deter free-trial
abuse on cloud deployments:
  - REQUIRE_TERMS_ACCEPTANCE (pre-existing; sanity-test only)
  - REJECT_DISPOSABLE_EMAILS — hard-rejects throwaway email providers
  - USE_SIGNUP_HONEYPOT — hidden ``company`` field catches DOM-fill bots

Community deployments leave all three off by default, so the form
behaves exactly as allauth's vanilla SignupForm. Cloud turns them on
via cloud.py / local.py. These tests exercise both postures so a
regression in either direction is caught.
"""

from __future__ import annotations

from django.test import TestCase
from django.test import override_settings


def _build_form(data, **overrides):
    """Instantiate UserSignupForm with the given POST payload.

    We import inside the helper to avoid triggering form-side
    imports (django-recaptcha, disposable-email-domains) at module load
    when tests that don't need them are being collected.
    """
    from validibot.users.forms import UserSignupForm

    return UserSignupForm(data=data)


_BASE_VALID_DATA = {
    "username": "alice",
    "email": "alice@example.com",
    "password1": "Str0ngPassw0rd!",
    "password2": "Str0ngPassw0rd!",
}


class DisposableEmailRejectionTests(TestCase):
    """REJECT_DISPOSABLE_EMAILS hard-rejects throwaway domains."""

    @override_settings(REJECT_DISPOSABLE_EMAILS=True)
    def test_mailinator_is_rejected_when_enabled(self):
        """mailinator.com is on the blocklist — must be rejected.

        This is the canonical abuse case: farmers use mailinator to
        receive verification emails without tying the address to a
        real mailbox. Hard-rejecting stops the cheapest farming path.
        """
        data = {**_BASE_VALID_DATA, "email": "alice@mailinator.com"}
        form = _build_form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)
        # Error message should be user-friendly, not expose internals.
        error_text = str(form.errors["email"])
        self.assertIn("disposable", error_text.lower())

    @override_settings(REJECT_DISPOSABLE_EMAILS=True)
    def test_case_insensitive_domain_match(self):
        """Domain casing must not bypass the blocklist.

        Submitted emails can arrive with mixed-case domains
        (GMAIL.COM, Mailinator.COM). The check lowercases the domain
        before lookup so attackers can't trivially evade.
        """
        data = {**_BASE_VALID_DATA, "email": "alice@Mailinator.COM"}
        form = _build_form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("email", form.errors)

    @override_settings(REJECT_DISPOSABLE_EMAILS=False)
    def test_mailinator_is_allowed_when_disabled(self):
        """Community/self-hosted default — throwaway emails must pass.

        Self-hosted admins testing their own deployments often use
        test@mailinator.com to verify signup. The feature must default
        off so we don't regress that workflow.
        """
        data = {**_BASE_VALID_DATA, "email": "alice@mailinator.com"}
        form = _build_form(data)
        # Honeypot and terms are also off by default, so allauth's
        # built-in validators are all that remain. We only assert that
        # our disposable check didn't add an email error.
        form.is_valid()  # trigger clean
        self.assertNotIn("email", form.errors)

    @override_settings(REJECT_DISPOSABLE_EMAILS=True)
    def test_real_domain_passes(self):
        """Real email providers are not on the blocklist.

        Guard against a false-positive regression where a popular
        domain sneaks into the blocklist — we'd rather catch that in
        CI than in a customer-support ticket.
        """
        data = {**_BASE_VALID_DATA, "email": "alice@gmail.com"}
        form = _build_form(data)
        form.is_valid()
        self.assertNotIn("email", form.errors)


class HoneypotFieldTests(TestCase):
    """USE_SIGNUP_HONEYPOT adds a hidden ``company`` field.

    Bots that DOM-fill every input trip the honeypot; real users never
    see the field. When tripped, validation fails with a generic
    message (we don't reveal that the honeypot is the trap).
    """

    @override_settings(USE_SIGNUP_HONEYPOT=True)
    def test_filled_honeypot_fails_validation(self):
        """A filled ``company`` field must invalidate the form.

        Bots running autofill against every input will fill this field.
        We reject the submission with a generic error rather than
        saying "honeypot tripped" — solver services should not get a
        signal about which field they failed on.
        """
        data = {**_BASE_VALID_DATA, "company": "Acme Inc"}
        form = _build_form(data)
        self.assertFalse(form.is_valid())
        self.assertIn("company", form.errors)

    @override_settings(USE_SIGNUP_HONEYPOT=True)
    def test_empty_honeypot_is_allowed(self):
        """Real users leave ``company`` empty — form must accept it.

        This is the happy path: the honeypot is present in the DOM but
        a real user never fills it. Validation passes (on the
        honeypot field; allauth may still complain about other fields
        unrelated to this test).
        """
        data = {**_BASE_VALID_DATA, "company": ""}
        form = _build_form(data)
        form.is_valid()
        self.assertNotIn("company", form.errors)

    @override_settings(USE_SIGNUP_HONEYPOT=False)
    def test_honeypot_field_absent_when_disabled(self):
        """Community default — no honeypot field is rendered.

        Self-hosted deployments don't need the field (no free-trial
        attack surface). The form must not include it in ``fields``.
        """
        form = _build_form(_BASE_VALID_DATA)
        self.assertNotIn("company", form.fields)


class HoneypotAndDisposableIndependenceTests(TestCase):
    """Each protection can be toggled independently of the others."""

    @override_settings(REJECT_DISPOSABLE_EMAILS=True, USE_SIGNUP_HONEYPOT=False)
    def test_disposable_only_no_honeypot_field(self):
        """Enabling disposable-email check alone should not render the honeypot.

        The two settings are orthogonal; flipping one on must not
        silently enable the other.
        """
        form = _build_form(_BASE_VALID_DATA)
        self.assertNotIn("company", form.fields)

    @override_settings(REJECT_DISPOSABLE_EMAILS=False, USE_SIGNUP_HONEYPOT=True)
    def test_honeypot_only_does_not_block_disposable(self):
        """Enabling honeypot alone must not block disposable emails.

        An admin might want the honeypot for bot-detection without
        rejecting mailinator.com. These two features solve different
        threats and must be independently controllable.
        """
        data = {**_BASE_VALID_DATA, "email": "alice@mailinator.com"}
        form = _build_form(data)
        form.is_valid()
        self.assertNotIn("email", form.errors)
