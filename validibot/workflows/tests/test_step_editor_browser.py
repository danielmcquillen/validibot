"""Browser regressions for workflow authoring layouts.

These opt-in tests cover editor geometry that response tests cannot observe.
Workflow and step detail pages must share resizable-column behavior, while long
settings forms must keep their action footer visible and hand vertical scrolling
to the card body.
"""

from __future__ import annotations

import os
from unittest import skipUnless

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from selenium import webdriver
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import WebDriverWait

from validibot.users.constants import RoleCode
from validibot.users.tests.utils import ensure_all_roles_exist
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory


@skipUnless(
    os.environ.get("RUN_BROWSER_TESTS") == "1",
    "Set RUN_BROWSER_TESTS=1 to run Selenium browser tests.",
)
class WorkflowEditorBrowserTests(StaticLiveServerTestCase):
    """Verify shared workflow editor column behavior in a real browser."""

    @classmethod
    def setUpClass(cls):
        """Start one desktop-sized headless Chrome session for this suite."""
        super().setUpClass()
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1440,1200")
        options.add_argument("--disable-gpu")
        cls.driver = webdriver.Chrome(options=options)
        cls.wait = WebDriverWait(cls.driver, 10)

    @classmethod
    def tearDownClass(cls):
        """Close Chrome even when a browser assertion fails."""
        cls.driver.quit()
        super().tearDownClass()

    def setUp(self):
        """Create an authenticated author with an assertion-capable step."""
        ensure_all_roles_exist()
        self.workflow = WorkflowFactory()
        self.user = self.workflow.user
        self.org = self.workflow.org
        membership = self.user.memberships.get(org=self.org)
        membership.set_roles({RoleCode.AUTHOR})
        self.user.set_current_org(self.org)
        self.step = WorkflowStepFactory(
            workflow=self.workflow,
            validator=ValidatorFactory(
                validation_type=ValidationType.CUSTOM_VALIDATOR,
                supports_assertions=True,
            ),
        )

        session = self.client.session
        session["active_org_id"] = self.org.pk
        session.save()
        self.client.force_login(self.user)

        self.driver.get(self.live_server_url)
        self.driver.execute_script(
            """
            localStorage.removeItem('validibot:resizable:workflow-detail-v2');
            localStorage.removeItem('validibot:resizable:step-detail-v2');
            """,
        )
        self.driver.add_cookie(
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": self.client.cookies[settings.SESSION_COOKIE_NAME].value,
                "path": "/",
            },
        )

    def test_separator_is_visible_and_resizes_step_editor_columns(self):
        """Authors must see and be able to drag the column separator."""
        edit_url = reverse(
            "workflows:workflow_step_edit",
            args=[self.workflow.pk, self.step.pk],
        )
        self.driver.get(f"{self.live_server_url}{edit_url}")

        handle = self.wait.until(
            lambda driver: driver.find_element(By.CSS_SELECTOR, ".resizable-handle"),
        )
        left_panel = self.driver.find_elements(
            By.CSS_SELECTOR,
            "[data-resizable-columns] > .resizable-panel",
        )[0]
        initial_width = left_panel.size["width"]

        self.assertTrue(handle.is_displayed())
        ActionChains(self.driver).drag_and_drop_by_offset(handle, 100, 0).perform()

        self.wait.until(lambda _driver: left_panel.size["width"] > initial_width)
        self.assertGreater(left_panel.size["width"], initial_width)

    def test_icon_only_add_assertion_opens_direct_modal(self):
        """The compact header action still loads non-Tabular assertion forms.

        This browser check protects the direct HTMx branch while the separate
        Tabular suite protects the stage-chooser branch.
        """
        edit_url = reverse(
            "workflows:workflow_step_edit",
            args=[self.workflow.pk, self.step.pk],
        )
        self.driver.get(f"{self.live_server_url}{edit_url}")
        add_button = self.wait.until(
            ec.element_to_be_clickable(
                (
                    By.CSS_SELECTOR,
                    '#assertions-editor-card button[aria-label="Add assertion"]',
                ),
            ),
        )
        tooltip = add_button.find_element(By.XPATH, "..")

        self.assertEqual(tooltip.get_attribute("data-bs-toggle"), "tooltip")
        self.assertEqual(
            tooltip.get_attribute("data-bs-original-title")
            or tooltip.get_attribute("title"),
            "Add assertion",
        )
        self.assertEqual(add_button.get_attribute("data-bs-toggle"), "modal")
        self.assertEqual(
            add_button.get_attribute("data-bs-target"),
            "#workflowAssertionModal",
        )
        self.assertTrue(
            add_button.find_element(By.CSS_SELECTOR, ".bi-plus-lg"),
        )

        ActionChains(self.driver).move_to_element(add_button).perform()
        tooltip_text = self.wait.until(
            ec.visibility_of_element_located((By.CSS_SELECTOR, ".tooltip-inner")),
        )
        self.assertEqual(tooltip_text.text, "Add assertion")

        add_button.click()
        modal = self.wait.until(
            ec.visibility_of_element_located((By.ID, "workflowAssertionModal")),
        )
        self.wait.until(
            lambda _driver: modal.find_elements(By.CSS_SELECTOR, "form"),
        )

    def test_workflow_and_step_editors_have_matching_default_geometry(self):
        """Both editor pages must start with the same split and divider space."""
        workflow_url = reverse(
            "workflows:workflow_detail",
            args=[self.workflow.pk],
        )
        step_url = reverse(
            "workflows:workflow_step_edit",
            args=[self.workflow.pk, self.step.pk],
        )

        workflow_geometry = self._layout_geometry(workflow_url)
        step_geometry = self._layout_geometry(step_url)

        self.assertAlmostEqual(
            workflow_geometry["left_ratio"],
            step_geometry["left_ratio"],
            places=2,
        )
        self.assertEqual(
            workflow_geometry["divider_space"],
            step_geometry["divider_space"],
        )

    def test_long_step_settings_pin_footer_and_scroll_card_body(self):
        """Laptop-height XML settings should scroll inside the card body only."""
        validator = ValidatorFactory(validation_type=ValidationType.XML_SCHEMA)
        ruleset = RulesetFactory(
            org=self.org,
            ruleset_type=RulesetType.XML_SCHEMA,
        )
        step = WorkflowStepFactory(
            workflow=self.workflow,
            validator=validator,
            ruleset=ruleset,
        )
        settings_url = reverse(
            "workflows:workflow_step_settings",
            args=[self.workflow.pk, step.pk],
        )
        original_size = self.driver.get_window_size()
        try:
            self.driver.set_window_size(1440, 800)
            self.driver.get(f"{self.live_server_url}{settings_url}")
            self.wait.until(
                ec.presence_of_element_located((By.CSS_SELECTOR, ".editor-card")),
            )

            metrics = self.driver.execute_script(
                """
                const footer = document.querySelector('.editor-card > .card-footer');
                const body = document.querySelector('.editor-card__scroll');
                return {
                  pageOverflow: getComputedStyle(document.body).overflow,
                  footerBottom: footer.getBoundingClientRect().bottom,
                  viewportHeight: window.innerHeight,
                  footerGap: window.innerHeight - footer.getBoundingClientRect().bottom,
                  bodyClientHeight: body.clientHeight,
                  bodyScrollHeight: body.scrollHeight,
                  bodyOverflowY: getComputedStyle(body).overflowY,
                };
                """,
            )

            self.assertEqual(metrics["pageOverflow"], "hidden")
            self.assertLessEqual(
                metrics["footerBottom"],
                metrics["viewportHeight"] + 1,
            )
            self.assertAlmostEqual(metrics["footerGap"], 5, delta=1)
            self.assertEqual(metrics["bodyOverflowY"], "auto")
            self.assertGreater(
                metrics["bodyScrollHeight"],
                metrics["bodyClientHeight"],
            )
        finally:
            self.driver.set_window_size(
                original_size["width"],
                original_size["height"],
            )

    def _layout_geometry(self, path):
        """Return the rendered panel ratio and divider space for one editor."""
        self.driver.get(f"{self.live_server_url}{path}")
        self.wait.until(
            lambda driver: driver.find_element(
                By.CSS_SELECTOR,
                '[data-resizable-init="true"]',
            ),
        )
        return self.driver.execute_script(
            """
            const container = document.querySelector('[data-resizable-columns]');
            const panels = container.querySelectorAll(':scope > .resizable-panel');
            const handle = container.querySelector(':scope > .resizable-handle');
            const style = getComputedStyle(handle);
            return {
              left_ratio: panels[0].getBoundingClientRect().width /
                (panels[0].getBoundingClientRect().width +
                 panels[1].getBoundingClientRect().width),
              divider_space: handle.getBoundingClientRect().width +
                parseFloat(style.marginLeft) +
                parseFloat(style.marginRight),
            };
            """,
        )
