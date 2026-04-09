"""CSP-compatible reCAPTCHA v3 widget.

The standard django-recaptcha ``ReCaptchaV3`` widget renders inline
``<script>`` tags that are blocked by our Content Security Policy.
This widget renders only the external API script tag and the hidden
input — the form submission handler lives in our bundled TypeScript
(``recaptcha.ts``) which already has a CSP nonce via the ``<script>``
tag in ``core/base.html``.

See: https://github.com/praekelt/django-recaptcha/issues/101
"""

from __future__ import annotations

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
