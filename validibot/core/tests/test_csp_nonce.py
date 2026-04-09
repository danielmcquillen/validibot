"""Tests for CSP nonce injection on public-facing pages.

Every ``<script>`` tag rendered by our templates must have a non-empty
``nonce`` attribute so the Content Security Policy allows it to execute.
A missing or empty nonce causes the browser to silently block the script,
breaking features like reCAPTCHA (login/signup) and PostHog (analytics).

These tests catch regressions where:
- A new inclusion tag returns a context without ``CSP_NONCE``
- A third-party widget renders inline scripts without nonces
- A template override loses the nonce during reformatting

The tests use Django's test client (which runs the full middleware stack
including ``CSPMiddleware``) and parse the HTML response for script tags.
"""

from __future__ import annotations

import re

import pytest
from django.test import Client
from django.urls import reverse


def _extract_script_nonces(html: str) -> list[str]:
    """Return the nonce attribute value for every <script> tag in the HTML.

    Returns empty string for script tags with ``nonce=""`` or no nonce
    attribute at all, so the test can assert all values are non-empty.
    """
    # Match <script ...> tags (not </script>).
    script_tags = re.findall(r"<script\b[^>]*>", html, re.IGNORECASE)
    nonces = []
    for tag in script_tags:
        # External scripts (with a src= attribute) don't need nonces —
        # CSP allows them via the domain whitelist ('self', posthog, etc.).
        # Only inline scripts (no src=) must have nonces.
        if "src=" in tag:
            continue
        match = re.search(r'nonce="([^"]*)"', tag)
        nonces.append(match.group(1) if match else "")
    return nonces


@pytest.mark.django_db
class TestCSPNoncesOnPublicPages:
    """Verify that all script tags on public pages have non-empty CSP nonces.

    These pages are tested because they're the most likely to break:
    - Login page: has reCAPTCHA + PostHog
    - Signup page: has reCAPTCHA + PostHog
    """

    def test_login_page_scripts_have_nonces(self):
        """Every inline <script> on the login page must have a non-empty
        nonce attribute.

        The reCAPTCHA widget and PostHog tracker both render scripts that
        need CSP nonces. If any script has nonce="" or no nonce at all,
        it will be blocked by the browser's CSP enforcement.
        """
        client = Client()
        response = client.get(reverse("account_login"))
        html = response.content.decode()
        nonces = _extract_script_nonces(html)

        assert len(nonces) > 0, "Expected at least one script tag on the login page"
        for i, nonce in enumerate(nonces):
            assert nonce, (
                f"Script tag #{i + 1} on the login page has an empty or "
                f"missing nonce. This will cause a CSP violation in the "
                f"browser. Check that the template passes CSP_NONCE to "
                f"all inclusion tags and widget templates."
            )

    def test_signup_page_scripts_have_nonces(self):
        """Every inline <script> on the signup page must have a non-empty
        nonce attribute.

        Same rationale as the login page test — reCAPTCHA and PostHog
        scripts need nonces to pass CSP.
        """
        client = Client()
        response = client.get(reverse("account_signup"))
        html = response.content.decode()
        nonces = _extract_script_nonces(html)

        assert len(nonces) > 0, "Expected at least one script tag on the signup page"
        for i, nonce in enumerate(nonces):
            assert nonce, (
                f"Script tag #{i + 1} on the signup page has an empty or "
                f"missing nonce. This will cause a CSP violation in the "
                f"browser. Check that the template passes CSP_NONCE to "
                f"all inclusion tags and widget templates."
            )
