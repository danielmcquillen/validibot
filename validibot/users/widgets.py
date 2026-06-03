"""Anti-abuse widgets for the signup form.

This module holds the widgets that harden signup against bots and spam:

* :class:`CSPSafeReCaptchaV3` — reCAPTCHA v3 with no inline JavaScript so it
  passes our nonce-based Content Security Policy.
* :class:`HoneypotInput` — an off-screen text input that traps autofill bots.

reCAPTCHA background: the standard django-recaptcha ``ReCaptchaV3`` widget
renders inline ``<script>`` tags that are blocked by our Content Security
Policy. ``CSPSafeReCaptchaV3`` renders only the external API script tag and
the hidden input — the form submission handler lives in our bundled TypeScript
(``recaptcha.ts``) which already has a CSP nonce via the ``<script>`` tag in
``core/base.html``.

See: https://github.com/praekelt/django-recaptcha/issues/101
"""

from __future__ import annotations

from django import forms
from django_recaptcha.widgets import ReCaptchaV3


class CSPSafeReCaptchaV3(ReCaptchaV3):
    """reCAPTCHA v3 widget that does not render inline JavaScript.

    Renders only:
    - The external ``api.js`` script tag (allowed by CSP domain whitelist)
    - The hidden ``<input>`` that carries the reCAPTCHA token

    The form submit interception and ``grecaptcha.execute()`` call are
    handled by ``recaptcha.ts`` in the compiled JS bundle.
    """

    template_name = "users/widgets/recaptcha_v3.html"


class HoneypotInput(forms.TextInput):
    """A text input that acts as a spam honeypot — invisible to real users.

    Renders an ordinary ``type="text"`` ``<input>`` (deliberately NOT
    ``type="hidden"`` and NOT ``display:none``) that is positioned far
    off-screen and removed from both the tab order and the accessibility
    tree. Real users never see, focus, or fill it; bots that blindly autofill
    every input do — which a paired ``clean_<field>`` method can then reject.

    Why these specific techniques (each one matters):

    * **Off-screen, not** ``display:none`` **/** ``type="hidden"`` — spam
      "solver" services specifically skip inputs hidden those ways, so a
      conventionally-hidden honeypot catches nothing. An input that is present
      and ``type="text"`` but moved off-screen looks real to a naive
      DOM-filler.
    * ``aria-hidden="true"`` **+** ``tabindex="-1"`` — keep screen-reader and
      keyboard users from ever reaching the field so they cannot trip the trap
      by accident.
    * ``autocomplete="off"`` — discourage password managers from filling it.

    **Rendering contract — important.** Render this field *raw*
    (``{{ form.company }}`` / ``{{ field }}``), never through crispy's
    ``as_crispy_field`` filter or a ``{% crispy %}`` layout. Those emit a
    sibling ``<label>`` that sits *outside* the input and is therefore not
    covered by the input's own off-screen hiding — the label would show up as
    a stray, field-less row in the form (the exact bug this widget exists to
    prevent). A widget can hide *itself*; it cannot hide a label another
    renderer adds beside it. Pair the field with ``label=""`` as
    belt-and-suspenders.

    Reusable on any form that wants a honeypot::

        company = forms.CharField(
            required=False,
            label="",
            widget=HoneypotInput(),
        )
    """

    # Off-screen positioning keeps the field in the DOM (so bots still find and
    # fill it) while keeping it out of sight. Defined here once so every
    # honeypot looks identical and templates need no hiding markup of their own.
    OFFSCREEN_STYLE = "position:absolute;left:-10000px;"

    def __init__(self, attrs: dict | None = None) -> None:
        # Start from the honeypot defaults, then let any caller-supplied attrs
        # override them (e.g. a different off-screen technique on another form).
        honeypot_attrs: dict = {
            "autocomplete": "off",
            "tabindex": "-1",
            "aria-hidden": "true",
            "style": self.OFFSCREEN_STYLE,
        }
        if attrs:
            honeypot_attrs.update(attrs)
        super().__init__(honeypot_attrs)
