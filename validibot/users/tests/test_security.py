"""Tests for the user Security settings page and MFA wiring.

Validibot exposes django-allauth's MFA feature through a Validibot-branded
landing page at ``/users/security/``. The heavy lifting (TOTP activation,
recovery-code generation, login challenges) is handled by allauth itself and
is already covered by allauth's own test suite — we deliberately do **not**
re-test those flows here.

What we cover in this file:

- The ``UserSecurityView`` page renders for authenticated users, redirects
  anonymous ones, and exposes the right context flags based on which
  authenticators the user has configured.
- The "Security" link appears in both the site top-nav dropdown and the
  in-app top bar dropdown so it can't silently go missing after template
  edits.
- The ``user_settings_nav_state`` template tag highlights the Security tab
  during the Validibot landing page *and* throughout the allauth MFA flow
  (``mfa_*`` URL names), so a user setting up TOTP doesn't feel like they've
  left the settings area.
- Each allauth MFA leaf template (TOTP activate/deactivate, recovery
  codes view/generate) has a Validibot-branded override that extends
  ``app_base.html`` directly, so these pages render inside the in-app
  chrome with breadcrumbs and a "Back to Security" link.
- The ``mfa_breadcrumbs`` template tag builds the standard breadcrumb
  trail used by all four leaf templates.

Any new assertion here should be about Validibot-specific wiring; trust
allauth for the cryptographic and state-machine parts.
"""

from http import HTTPStatus

import pytest
from allauth.mfa.models import Authenticator
from allauth.mfa.totp.internal.auth import TOTP
from django.template.loader import get_template
from django.urls import reverse

from validibot.core.templatetags.core_tags import user_settings_nav_state
from validibot.users.models import User

pytestmark = pytest.mark.django_db


# ── Access control ────────────────────────────────────────────────────
# The Security page contains sensitive configuration (MFA setup, recovery
# codes) and must not leak anything to anonymous visitors.


class TestUserSecurityViewAccess:
    """The Security settings page is for logged-in users only."""

    def test_anonymous_user_is_redirected_to_login(self, client):
        """Unauthenticated GET must bounce to login, not 200 empty content.

        A missing login gate would expose MFA status to anyone who knew the
        URL — minor on its own, but it's the kind of hole that compounds.
        """
        response = client.get(reverse("users:security"))
        assert response.status_code == HTTPStatus.FOUND
        assert reverse("account_login") in response.url

    def test_authenticated_user_sees_page(self, client, user: User):
        """Logged-in users should get a 200 and the Security template."""
        client.force_login(user)
        response = client.get(reverse("users:security"))
        assert response.status_code == HTTPStatus.OK
        assert "users/security.html" in [t.name for t in response.templates]


# ── Context flags ─────────────────────────────────────────────────────
# The template branches on ``totp_enabled`` / ``is_mfa_enabled`` to decide
# which buttons and cards to render. If those flags are wrong, the user
# sees stale state — "Activate" when they already have TOTP on, or no
# recovery codes section when they should be able to regenerate.


class TestUserSecurityViewContext:
    """The view exposes enough context flags to drive the template."""

    def test_defaults_when_no_mfa_configured(self, client, user: User):
        """Brand-new user has no authenticators — every flag defaults off."""
        client.force_login(user)
        response = client.get(reverse("users:security"))
        ctx = response.context
        assert ctx["is_mfa_enabled"] is False
        assert ctx["totp_enabled"] is False
        assert ctx["recovery_codes"] is None
        assert ctx["authenticators"] == {}

    def test_reports_totp_enabled_after_activation(
        self,
        client,
        user: User,
    ):
        """Once TOTP is activated, the page must flip to the "Active" branch.

        We use allauth's own ``TOTP.activate()`` helper to avoid minting the
        encrypted-secret JSON by hand — keeps the test honest about the real
        storage shape.
        """
        TOTP.activate(user, secret="JBSWY3DPEHPK3PXP")  # noqa: S106 — test secret
        client.force_login(user)

        response = client.get(reverse("users:security"))
        ctx = response.context

        assert ctx["is_mfa_enabled"] is True
        assert ctx["totp_enabled"] is True
        assert Authenticator.Type.TOTP in ctx["authenticators"]


# ── Dropdown menus ────────────────────────────────────────────────────
# The Security link appears in two menus and both matter. If either
# regresses, users lose discoverability of a security feature — bad.


class TestSecurityMenuLinks:
    """The Security link is present in both user-facing menus.

    The two nav partials live on *different* base templates — ``top_nav.html``
    wraps pages using ``core/base.html`` (marketing / logged-out style), and
    ``app_top_bar.html`` wraps pages using ``app_base.html`` (the in-app
    chrome). A single request only renders one of them, so we check template
    source directly to catch accidental removal from either partial.
    """

    def test_site_top_nav_partial_contains_security_link(self):
        """Outer site nav partial references ``users:security``."""
        source = get_template("core/partial/top_nav.html").template.source
        assert "'users:security'" in source
        assert "Security" in source

    def test_in_app_top_bar_partial_contains_security_link(self):
        """In-app top bar partial references ``users:security``.

        This is the dropdown most logged-in users interact with, so if the
        link is missing here the feature is effectively hidden.
        """
        source = get_template(
            "app/partial/components/app_top_bar.html",
        ).template.source
        assert "'users:security'" in source
        assert "Security" in source

    def test_security_link_rendered_on_page(self, client, user: User):
        """Live page render includes at least one working link to Security.

        Belt-and-braces: if the template tag / URL namespacing ever breaks,
        the partial-source tests above wouldn't catch it, but this one will.
        """
        client.force_login(user)
        response = client.get(reverse("users:security"))
        assert reverse("users:security") in response.content.decode()


# ── Settings nav state ────────────────────────────────────────────────
# The user_settings_nav_state template tag tells every settings template
# which tab is active. If it doesn't recognise the new "security" key or
# the allauth mfa_* URL names, the tab won't highlight during setup flows
# and the page feels disconnected.


class TestSettingsNavState:
    """``user_settings_nav_state`` recognises Security and allauth MFA flows."""

    def _state_for(self, url_name: str, view_name: str = "") -> dict:
        """Build a minimal request context and invoke the template tag."""

        class StubResolver:
            def __init__(self, url_name: str, view_name: str) -> None:
                self.url_name = url_name
                self.view_name = view_name or url_name

        class StubRequest:
            def __init__(self, url_name: str, view_name: str) -> None:
                self.resolver_match = StubResolver(url_name, view_name)

        return user_settings_nav_state(
            {"request": StubRequest(url_name, view_name)},
        )

    def test_security_tab_active_on_own_url(self):
        """The ``users:security`` URL activates the Security tab."""
        state = self._state_for("security", "users:security")
        assert state["security"] is True
        assert state["active"] is True

    def test_security_tab_stays_active_during_totp_activation(self):
        """During ``mfa_activate_totp`` the Security tab must stay highlighted.

        Otherwise users feel like they've been dumped out of settings
        mid-flow.
        """
        state = self._state_for("mfa_activate_totp", "mfa_activate_totp")
        assert state["security"] is True

    def test_security_tab_stays_active_on_recovery_codes(self):
        """Same story for recovery-code views."""
        state = self._state_for(
            "mfa_view_recovery_codes",
            "mfa_view_recovery_codes",
        )
        assert state["security"] is True

    def test_security_tab_not_active_on_profile(self):
        """Guard against over-eager matching — profile shouldn't light it up."""
        state = self._state_for("profile", "users:profile")
        assert state["security"] is False
        assert state["profile"] is True


# ── Allauth template overrides ────────────────────────────────────────
# Each allauth MFA management page has a Validibot-branded override
# template that extends `app_base.html` directly (matching the pattern
# used by users/security.html). If one of these overrides goes missing,
# allauth's upstream version takes over and the page loses its in-app
# chrome entirely.
#
# We explicitly override the leaf templates instead of allauth's
# `mfa/base_manage.html` because Django's block inheritance doesn't
# compose cleanly through allauth's base_manage layer — allauth's leaf
# templates redefine `{% block content %}`, which erases any wrapper
# chrome we add at the base layer.


class TestMfaLeafTemplateOverrides:
    """Each allauth MFA leaf template has a Validibot-branded override."""

    LEAF_TEMPLATES = [
        "mfa/totp/activate_form.html",
        "mfa/totp/deactivate_form.html",
        "mfa/recovery_codes/index.html",
        "mfa/recovery_codes/generate.html",
    ]

    @pytest.mark.parametrize("template_name", LEAF_TEMPLATES)
    def test_override_extends_app_base(self, template_name):
        """Every override must extend ``app_base.html`` — that's the
        Validibot in-app chrome (left nav, top bar, user menu). If a
        template extends an allauth layout instead, the page renders
        without sidebar or top bar.
        """
        template = get_template(template_name)
        source = template.template.source
        assert '{% extends "app_base.html" %}' in source, (
            f"{template_name} must extend app_base.html for in-app chrome"
        )
        # And include our app left nav so the settings dropdown is present.
        assert "app_left_nav.html" in source, (
            f"{template_name} must include the app left nav"
        )

    @pytest.mark.parametrize("template_name", LEAF_TEMPLATES)
    def test_override_sets_top_bar_breadcrumbs(self, template_name):
        """Allauth MFA views don't run through our ``BreadcrumbMixin``,
        so the top-bar breadcrumb partial would render nothing. Each
        override uses the ``mfa_breadcrumbs`` tag to build the trail
        and re-includes the top-bar partial with ``breadcrumbs`` set.

        If the tag call disappears, users land on these pages with no
        sense of where they are in the settings hierarchy.
        """
        template = get_template(template_name)
        source = template.template.source
        assert "mfa_breadcrumbs" in source, (
            f"{template_name} must call the mfa_breadcrumbs tag"
        )
        assert "block top_bar" in source, (
            f"{template_name} must override block top_bar to inject breadcrumbs"
        )

    @pytest.mark.parametrize("template_name", LEAF_TEMPLATES)
    def test_override_provides_back_to_security_link(self, template_name):
        """Every override must expose a "Back to Security" action so
        users can escape the sub-flow without hunting for the left-nav
        entry.
        """
        template = get_template(template_name)
        source = template.template.source
        assert "Back to Security" in source, (
            f"{template_name} must include a Back to Security link"
        )
        assert "users:security" in source, (
            f"{template_name} Back link must target users:security"
        )


class TestMfaIndexRedirect:
    """``/accounts/2fa/`` redirects to our Security page, not allauth's index.

    Allauth ships its own MFA landing page at ``/accounts/2fa/`` that
    duplicates our ``/app/users/security/`` page with worse styling, and
    its post-action redirects (e.g. after deactivating TOTP) hard-code
    ``reverse("mfa_index")``. We override the ``mfa_index`` URL name in
    our URLconf so every such redirect lands on our branded Security
    page instead. These tests pin that behaviour in place.
    """

    def test_mfa_index_url_resolves_to_our_redirect(self, client, user: User):
        """``/accounts/2fa/`` returns a redirect to users:security.

        A regression here (e.g. if the URL include order flips and
        allauth's ``mfa_index`` wins) would drop users onto an
        unbranded page after every MFA action.
        """
        client.force_login(user)
        response = client.get(reverse("mfa_index"))
        assert response.status_code == HTTPStatus.FOUND
        assert response.url == reverse("users:security")
        # Allauth's mfa/index.html must never render here — if it did,
        # the user would see an unbranded duplicate of Security.
        assert "mfa/index.html" not in [t.name for t in response.templates if t.name]

    def test_mfa_index_redirect_survives_post_deactivation_flow(
        self,
        client,
        user: User,
    ):
        """Post-deactivation, allauth redirects to ``mfa_index``. Our
        URL override catches that and bounces to Security.

        We don't exercise the full allauth deactivation POST here
        (that requires reauthentication ceremony), but we pin the
        critical piece: chasing the redirect chain lands on Security,
        not on allauth's index template.
        """
        client.force_login(user)
        # Simulate the final step of allauth's post-action redirect:
        # allauth calls HttpResponseRedirect(reverse("mfa_index")),
        # which is the URL we intercept.
        response = client.get(reverse("mfa_index"), follow=True)
        assert response.status_code == HTTPStatus.OK
        assert "users/security.html" in [t.name for t in response.templates if t.name]
        assert "mfa/index.html" not in [t.name for t in response.templates if t.name]

    def test_reverse_mfa_index_points_at_2fa_path(self):
        """Sanity-check: ``reverse("mfa_index")`` resolves to our path,
        not allauth's. Allauth's internal redirects all call
        ``reverse("mfa_index")``, so if this flips, the user experience
        regresses even if /accounts/2fa/ itself still works.
        """
        assert reverse("mfa_index") == "/accounts/2fa/"


class TestMfaBreadcrumbsTag:
    """The ``mfa_breadcrumbs`` template tag builds the standard trail."""

    def test_returns_three_level_trail(self, rf):
        """The tag returns User Settings › Security › {leaf} — the shape
        every allauth MFA management page uses for breadcrumbs.
        """
        from validibot.core.templatetags.core_tags import mfa_breadcrumbs

        expected_length = 3  # User Settings, Security, leaf
        request = rf.get("/")
        result = mfa_breadcrumbs({"request": request}, "Activate Authenticator App")
        assert len(result) == expected_length
        assert str(result[0]["name"]) == "User Settings"
        assert str(result[1]["name"]) == "Security"
        assert result[2]["name"] == "Activate Authenticator App"
        # Leaf has no URL (it's the current page).
        assert result[2]["url"] == ""

    def test_profile_and_security_urls_resolve(self, rf):
        """The first two breadcrumbs link to real URLs so users can
        navigate back up the settings hierarchy.
        """
        from validibot.core.templatetags.core_tags import mfa_breadcrumbs

        request = rf.get("/")
        result = mfa_breadcrumbs({"request": request}, "Leaf")
        # Not asserting exact path because reverse_with_org is org-scoped;
        # just ensure the URLs are non-empty strings.
        assert result[0]["url"]
        assert result[1]["url"]
