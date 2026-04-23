"""Gate + render tests for the advanced analytics dashboard.

This is a placeholder page today (no querysets, just a "coming soon"
card), so the tests focus on what matters for placeholder pages:
the access control is correct, the URL reverses, and the template
renders with the right title and organisation name.

Same access-matrix shape as the audit log tests, but against the
``ADVANCED_ANALYTICS`` flag rather than ``AUDIT_LOG`` — this is the
whole reason we split the two flags.
"""

from __future__ import annotations

from django.test import Client
from django.test import TestCase
from django.urls import reverse

from validibot.core.features import CommercialFeature
from validibot.core.license import Edition
from validibot.core.license import License
from validibot.core.license import set_license
from validibot.users.tests.factories import MembershipFactory
from validibot.users.tests.utils import ensure_all_roles_exist


def _login_with_membership(client: Client, membership) -> None:
    """Log a user in and mark ``active_org`` on their session.

    Mirrors ``validibot.audit.tests.test_views._login_with_membership``
    so the two suites exercise the same harness.
    """

    user = membership.user
    user.set_current_org(membership.org)
    client.force_login(user)
    session = client.session
    session["active_org_id"] = membership.org.id
    session.save()


def _pro_license_with_advanced_analytics() -> License:
    """Return a Pro license that advertises ADVANCED_ANALYTICS only.

    Deliberately does NOT advertise ``AUDIT_LOG``. Proves the two
    flags are independent: the dashboard works without the audit
    log flag (and vice versa).
    """

    return License(
        edition=Edition.PRO,
        features=frozenset({CommercialFeature.ADVANCED_ANALYTICS.value}),
    )


class AdvancedAnalyticsAccessTests(TestCase):
    """Gate behaviour for /app/analytics/."""

    @classmethod
    def setUpTestData(cls):
        ensure_all_roles_exist()

    def test_anonymous_redirects_to_login(self) -> None:
        """Anonymous traffic hits ``LoginRequiredMixin`` before the
        feature gate, so the redirect-to-login behaviour is the same
        regardless of license state.
        """

        response = self.client.get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response.url)

    def test_community_license_renders_404(self) -> None:
        """Without ``ADVANCED_ANALYTICS`` in the license, the page
        must 404 — community deployments should not see analytics
        dashboards exist.
        """

        set_license(License(edition=Edition.COMMUNITY))
        membership = MembershipFactory()
        _login_with_membership(self.client, membership)

        response = self.client.get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 404)

    def test_pro_license_without_flag_renders_404(self) -> None:
        """A Pro license that omits ``ADVANCED_ANALYTICS`` must 404.

        Proves the gate keys off the feature flag, not the edition —
        if we ever ship a tier that drops analytics, the page
        disappears without a code change.
        """

        set_license(License(edition=Edition.PRO, features=frozenset()))
        membership = MembershipFactory()
        _login_with_membership(self.client, membership)

        response = self.client.get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 404)

    def test_audit_log_flag_alone_does_not_unlock_dashboard(self) -> None:
        """Advertising only ``AUDIT_LOG`` must NOT grant the analytics
        page — this is the assertion that proves the two flags are
        independent gates, not aliases.
        """

        set_license(
            License(
                edition=Edition.PRO,
                features=frozenset({CommercialFeature.AUDIT_LOG.value}),
            ),
        )
        membership = MembershipFactory()
        _login_with_membership(self.client, membership)

        response = self.client.get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 404)

    def test_pro_license_with_flag_renders_placeholder(self) -> None:
        """Happy path: flag on, membership present, page renders.

        Asserts the placeholder copy ("Coming soon") appears so a
        future drop of the template file fails loudly — without this
        assertion the test would still pass on an empty page.
        """

        set_license(_pro_license_with_advanced_analytics())
        membership = MembershipFactory()
        _login_with_membership(self.client, membership)

        response = self.client.get(reverse("analytics:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Advanced analytics")
        self.assertContains(response, "Coming soon")
