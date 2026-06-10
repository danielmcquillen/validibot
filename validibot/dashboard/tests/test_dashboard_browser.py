"""Browser regression tests for dashboard widget loading.

These opt-in Selenium tests cover the browser behavior that response tests
cannot observe: HTMx must replace every loading placeholder and the bundled
Chart.js initializer must run for chart widgets after each swap.
"""

from __future__ import annotations

import os
from unittest import skipUnless

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

from validibot.tracking.tests.factories import TrackingEventFactory
from validibot.users.constants import RoleCode
from validibot.users.tests.factories import UserFactory
from validibot.users.tests.utils import ensure_all_roles_exist


@skipUnless(
    os.environ.get("RUN_BROWSER_TESTS") == "1",
    "Set RUN_BROWSER_TESTS=1 to run Selenium browser tests.",
)
class DashboardBrowserTests(StaticLiveServerTestCase):
    """Verify dashboard widgets load and initialize in a real browser."""

    @classmethod
    def setUpClass(cls):
        """Start one headless Chrome session for the browser test class."""
        super().setUpClass()
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1440,1200")
        options.add_argument("--disable-gpu")
        cls.driver = webdriver.Chrome(options=options)
        cls.wait = WebDriverWait(cls.driver, 10)

    @classmethod
    def tearDownClass(cls):
        """Close Chrome even when a dashboard assertion fails."""
        cls.driver.quit()
        super().tearDownClass()

    def setUp(self):
        """Create an authenticated admin with chart data in the active org."""
        ensure_all_roles_exist()
        self.user = UserFactory()
        self.org = self.user.orgs.first()
        self.user.set_current_org(self.org)
        self.user.memberships.get(org=self.org).add_role(RoleCode.ADMIN)
        TrackingEventFactory(
            org=self.org,
            project__org=self.org,
            user=self.user,
        )

        session = self.client.session
        session["active_org_id"] = self.org.pk
        session.save()
        self.client.force_login(self.user)

        self.driver.get(self.live_server_url)
        self.driver.add_cookie(
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": self.client.cookies[settings.SESSION_COOKIE_NAME].value,
                "path": "/",
            },
        )

    def _open_dashboard_and_wait(self):
        """Open the dashboard and wait until every placeholder is replaced."""
        dashboard_url = reverse("dashboard:my_dashboard")
        self.driver.get(f"{self.live_server_url}{dashboard_url}")
        self.wait.until(
            lambda driver: (
                not driver.find_elements(
                    By.XPATH,
                    "//*[contains(normalize-space(.), 'Loading insights')]",
                )
            ),
        )

    def _assert_dashboard_loaded(self):
        """Assert the rendered widgets and chart initializers are complete."""
        widgets = self.driver.find_elements(By.CSS_SELECTOR, "[data-dashboard-widget]")
        self.assertEqual(len(widgets), 4)
        initialized_charts = self.driver.find_elements(
            By.CSS_SELECTOR,
            'canvas[data-chart-initialized="1"]',
        )
        self.assertEqual(len(initialized_charts), 2)

    def test_widgets_replace_loading_state_and_initialize_charts(self):
        """The dashboard must not leave users staring at loading placeholders."""
        self._open_dashboard_and_wait()
        self._assert_dashboard_loaded()

    def test_back_navigation_restores_a_complete_dashboard(self):
        """A browser-restored page must refresh instead of reviving placeholders."""
        self._open_dashboard_and_wait()
        workflows_url = reverse("workflows:workflow_list")
        self.driver.get(f"{self.live_server_url}{workflows_url}")
        self.driver.back()
        self.wait.until(
            lambda driver: (
                not driver.find_elements(
                    By.XPATH,
                    "//*[contains(normalize-space(.), 'Loading insights')]",
                )
            ),
        )
        self._assert_dashboard_loaded()
