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

from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.test import TestCase
from django.test import override_settings

# The auto-assigned widget id Django gives the honeypot ``company`` field.
# A crispy-rendered label would point at it via ``for="id_company"`` — that
# leaking label was the original bug. Rendered raw, HoneypotInput emits only
# the bare ``<input id="id_company">`` with no label at all.
_HONEYPOT_DOM_ID = "id_company"

# The off-screen offset HoneypotInput applies (kept in sync with
# HoneypotInput.OFFSCREEN_STYLE) to hold the field in the DOM but out of sight.
_OFFSCREEN_STYLE = "position:absolute;left:-10000px;"


def _render_signup_partial():
    """Render the community signup partial with an unbound UserSignupForm.

    Imported lazily for the same reason as ``_build_form`` — to avoid pulling
    in form-side optional dependencies at module collection time.
    """
    from validibot.users.forms import UserSignupForm

    return render_to_string(
        "account/partial/sign_up_form.html",
        {"form": UserSignupForm()},
    )


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


# ── Honeypot RENDERING ──────────────────────────────────────────────────────
# The form-level tests above prove the honeypot's *logic* (a filled field is
# rejected). They cannot prove its *presentation*, yet the original bug was a
# rendering defect: crispy rendered the field as ``<label>Company</label>
# <input …>`` and only the input was hidden, so the label leaked into the
# visible form as a stray, field-less row. The fix renders the honeypot raw via
# HoneypotInput (no crispy → no label). A honeypot has two contracts — reject
# fills, and stay invisible to humans — so they need separate coverage.
class HoneypotRenderingTests(TestCase):
    """The honeypot must render with no visible label and a hidden input.

    Regression tests for the visible "Company" label with no field beneath it.
    HoneypotInput is rendered raw in the signup partial, so there is no crispy
    label to leak; the input itself is pushed off-screen and removed from the
    tab order and the accessibility tree.
    """

    @override_settings(USE_SIGNUP_HONEYPOT=True)
    def test_honeypot_renders_without_a_visible_label(self):
        """No ``<label>`` may point at the honeypot input.

        This is the exact regression: crispy rendered ``for="id_company"`` beside
        the off-screen input and it showed as a stray row. Rendering the field
        raw emits only the input, so a ``for="id_company"`` in the output means
        someone reintroduced crispy rendering for the honeypot — fail loudly.
        """
        html = _render_signup_partial()
        # The honeypot input itself must be present...
        self.assertIn('name="company"', html)
        # ...but nothing may render a label tied to it.
        self.assertNotIn(f'for="{_HONEYPOT_DOM_ID}"', html)

    @override_settings(USE_SIGNUP_HONEYPOT=True)
    def test_honeypot_input_hides_itself_from_sight_keyboard_and_screen_readers(self):
        """With no wrapper, the bare input must carry every hiding attribute.

        The input alone is now responsible for being invisible (off-screen),
        unreachable by keyboard (``tabindex=-1``), and absent from the
        accessibility tree (``aria-hidden``). A regression that drops any one of
        these would let a real user encounter the trap.
        """
        html = _render_signup_partial()
        self.assertIn('aria-hidden="true"', html)
        self.assertIn('tabindex="-1"', html)
        self.assertIn(_OFFSCREEN_STYLE, html)

    @override_settings(USE_SIGNUP_HONEYPOT=True)
    def test_honeypot_is_a_text_input_not_type_hidden(self):
        """The trap must render ``type="text"``, never ``type="hidden"``.

        Spam solver services skip ``type="hidden"`` inputs, so a honeypot that
        renders hidden catches nothing. Guard the deliberate choice of a
        visible-type input that is merely positioned off-screen.
        """
        html = _render_signup_partial()
        self.assertIn('<input type="text" name="company"', html)

    @override_settings(USE_SIGNUP_HONEYPOT=False)
    def test_no_honeypot_markup_rendered_when_disabled(self):
        """Community default — the partial must emit no honeypot markup.

        With the feature off the field is absent from the form, so neither the
        input id nor the field name should appear. Guards against accidentally
        hard-coding the honeypot into the template.
        """
        html = _render_signup_partial()
        self.assertNotIn(_HONEYPOT_DOM_ID, html)
        self.assertNotIn('name="company"', html)


# ── HoneypotInput widget ─────────────────────────────────────────────────────
# Unit tests for the reusable widget itself, independent of the signup form —
# it is meant to be dropped onto any form that wants a honeypot, so these pin
# the contract those other callers will rely on.
class HoneypotInputWidgetTests(SimpleTestCase):
    """HoneypotInput bakes the hiding attributes so callers don't have to."""

    def _widget(self, attrs=None):
        """Build a HoneypotInput, importing lazily to match the form's usage."""
        from validibot.users.widgets import HoneypotInput

        return HoneypotInput(attrs=attrs)

    def test_default_attrs_make_the_input_a_trap(self):
        """Out of the box the widget must hide the input every way that matters.

        Off-screen (sight), tabindex=-1 (keyboard), aria-hidden (assistive
        tech), autocomplete=off (password managers) — exactly the properties a
        paired clean_<field> check relies on real users never triggering.
        """
        attrs = self._widget().attrs
        self.assertEqual(attrs["autocomplete"], "off")
        self.assertEqual(attrs["tabindex"], "-1")
        self.assertEqual(attrs["aria-hidden"], "true")
        self.assertEqual(attrs["style"], _OFFSCREEN_STYLE)

    def test_renders_a_text_input_not_hidden(self):
        """The trap must render as a real text input, not ``type="hidden"``.

        Solver bots skip ``type="hidden"`` inputs, so the widget must emit an
        ordinary text input that merely sits off-screen and out of the a11y tree.
        """
        html = self._widget().render("company", "")
        self.assertIn('type="text"', html)
        self.assertNotIn('type="hidden"', html)
        self.assertIn('aria-hidden="true"', html)
        self.assertIn(_OFFSCREEN_STYLE, html)

    def test_caller_attrs_override_defaults(self):
        """A reusing form may override any baked-in attr while keeping the rest.

        The defaults are a convenience, not a straitjacket — explicit attrs win
        (matching Django widget conventions), and untouched defaults remain.
        """
        widget = self._widget(attrs={"style": "left:-9999px;", "data-x": "1"})
        self.assertEqual(widget.attrs["style"], "left:-9999px;")
        self.assertEqual(widget.attrs["data-x"], "1")
        # A default the caller did not override must still be present.
        self.assertEqual(widget.attrs["aria-hidden"], "true")
