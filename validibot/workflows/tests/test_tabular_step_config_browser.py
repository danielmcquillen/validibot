"""Browser regression tests for the Tabular settings editor.

These opt-in Selenium tests exercise the client behavior that Django response
tests cannot observe: rendered header consistency, HTMx swaps, focus placement,
DOM reordering, type-aware constraint visibility, primary-key coupling, and
preview/apply replacement. They are skipped during the normal fast suite and run when
``RUN_BROWSER_TESTS=1`` is set in an environment with Chrome available.
"""

from __future__ import annotations

import json
import os
from unittest import skipUnless

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from selenium import webdriver
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as ec
from selenium.webdriver.support.ui import Select
from selenium.webdriver.support.ui import WebDriverWait

from validibot.submissions.constants import SubmissionFileType
from validibot.users.tests.utils import ensure_all_roles_exist
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.validators.tabular.metadata import TABULAR_DATASET_INPUTS
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.tests.test_tabular_step_config import _login_as_author

# The left nav expands to 200px (flex: 0 0 200px). Anything at least this wide
# counts as "settled" after the width transition, with a px of sub-pixel slack.
_LEFT_NAV_SETTLED_WIDTH = 199


@skipUnless(
    os.environ.get("RUN_BROWSER_TESTS") == "1",
    "Set RUN_BROWSER_TESTS=1 to run Selenium browser tests.",
)
class TabularSettingsBrowserTests(StaticLiveServerTestCase):
    """Verify the editor's JavaScript and HTMx behavior in a real browser."""

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
        """Close Chrome even when an interaction assertion fails."""
        cls.driver.quit()
        super().tearDownClass()

    def setUp(self):
        """Create and authenticate one configured Tabular workflow step."""
        ensure_all_roles_exist()
        self.workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.TEXT],
        )
        self.validator = ValidatorFactory(
            validation_type=ValidationType.TABULAR,
            supports_assertions=True,
        )
        self.step = WorkflowStepFactory(
            workflow=self.workflow,
            validator=self.validator,
            ruleset=RulesetFactory(org=self.workflow.org),
        )
        self.step.ruleset.rules_text = json.dumps(
            {
                "fields": [
                    {"name": "site_id", "type": "string"},
                    {"name": "reading", "type": "number"},
                ],
            },
        )
        self.step.ruleset.save(update_fields=["rules_text"])
        _login_as_author(self.client, self.workflow)
        self.driver.get(self.live_server_url)
        self.driver.add_cookie(
            {
                "name": settings.SESSION_COOKIE_NAME,
                "value": self.client.cookies[settings.SESSION_COOKIE_NAME].value,
                "path": "/",
            },
        )
        path = reverse(
            "workflows:workflow_step_settings",
            kwargs={"pk": self.workflow.pk, "step_id": self.step.pk},
        )
        self.driver.get(f"{self.live_server_url}{path}")
        self.wait.until(
            ec.presence_of_element_located(
                (By.CSS_SELECTOR, "[data-tabular-column-editor]"),
            ),
        )
        for close_button in self.driver.find_elements(
            By.CSS_SELECTOR,
            ".toast .btn-close",
        ):
            self.driver.execute_script("arguments[0].click();", close_button)
        self.wait.until(
            lambda driver: all(
                not toast.is_displayed()
                for toast in driver.find_elements(By.CSS_SELECTOR, ".toast")
            ),
        )

    def _rows(self):
        """Return currently visible column rows in DOM order."""
        return [
            row
            for row in self.driver.find_elements(
                By.CSS_SELECTOR,
                "[data-tabular-column-row]",
            )
            if row.is_displayed()
        ]

    def _click(self, element):
        """Center a control in the viewport before using a real pointer click."""
        self.driver.execute_script(
            """
            arguments[0].scrollIntoView({
              behavior: 'instant',
              block: 'center',
              inline: 'nearest',
            });
            """,
            element,
        )
        self.wait.until(
            lambda driver: (
                element.is_displayed()
                and element.is_enabled()
                and driver.execute_script(
                    """
                const rect = arguments[0].getBoundingClientRect();
                return rect.top >= 0 && rect.bottom <= window.innerHeight;
                """,
                    element,
                )
            ),
        )
        element.click()

    def test_layout_uses_standard_nav_and_back_link_returns_to_step(self):
        """The settings workspace should use normal app navigation.

        The left navigation must collapse and expand exactly as it does on
        other app pages. Within the remaining content area, the main card uses
        the available width and the header reuses the step editor's compact
        back control without repeating its validator icon or type badge.
        """
        self.driver.set_window_size(2200, 1200)
        try:
            left_nav = self.driver.find_element(By.ID, "app-left-nav")
            nav_toggle = self.driver.find_element(By.ID, "app-left-nav-toggle")
            self.assertTrue(left_nav.is_displayed())
            self.assertEqual(nav_toggle.get_attribute("aria-expanded"), "true")

            self._click(nav_toggle)
            self.wait.until(
                lambda _driver: (
                    "is-collapsed" in left_nav.get_attribute("class").split()
                ),
            )
            self.assertEqual(nav_toggle.get_attribute("aria-expanded"), "false")

            self._click(nav_toggle)
            self.wait.until(
                lambda _driver: (
                    "is-collapsed" not in left_nav.get_attribute("class").split()
                ),
            )
            self.assertEqual(nav_toggle.get_attribute("aria-expanded"), "true")
            # The `is-collapsed` class drops immediately, but the nav width
            # animates back to 200px over ~0.28s. Wait for it to settle before
            # touching the header, or the still-sliding nav can intercept a
            # click at a now-stale coordinate.
            self.wait.until(
                lambda _driver: left_nav.rect["width"] >= _LEFT_NAV_SETTLED_WIDTH,
            )

            container = self.driver.find_element(By.ID, "tabular-step-settings")
            card = self.driver.find_element(By.CSS_SELECTOR, ".editor-card")
            self.assertGreater(card.rect["width"], container.rect["width"] * 0.95)
            self.assertFalse(
                self.driver.find_elements(
                    By.CSS_SELECTOR,
                    ".editor-card .badge.text-bg-primary",
                ),
            )

            back_link = self.driver.find_element(
                By.CSS_SELECTOR,
                '.app-content-header-bar a[aria-label="Back to workflow step"]',
            )
            back_button = back_link.find_element(By.XPATH, "..")
            settings_metrics = self.driver.execute_script(
                """
                const button = arguments[0];
                const rect = button.getBoundingClientRect();
                const style = getComputedStyle(button);
                return {
                  className: button.className,
                  width: rect.width,
                  height: rect.height,
                  borderRadius: style.borderRadius,
                  backgroundColor: style.backgroundColor,
                };
                """,
                back_button,
            )
            self.assertFalse(
                self.driver.find_elements(
                    By.CSS_SELECTOR,
                    ".app-content-header-bar .text-primary.fs-4",
                ),
            )
            expected_path = reverse(
                "workflows:workflow_step_edit",
                kwargs={"pk": self.workflow.pk, "step_id": self.step.pk},
            )
            self.assertEqual(
                back_link.get_attribute("href"),
                f"{self.live_server_url}{expected_path}",
            )

            self._click(back_link)
            self.wait.until(
                ec.url_to_be(f"{self.live_server_url}{expected_path}"),
            )
            step_link = self.wait.until(
                ec.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        ".app-content-header .title-wrapper > .btn > a",
                    ),
                ),
            )
            step_button = step_link.find_element(By.XPATH, "..")
            step_metrics = self.driver.execute_script(
                """
                const button = arguments[0];
                const rect = button.getBoundingClientRect();
                const style = getComputedStyle(button);
                return {
                  className: button.className,
                  width: rect.width,
                  height: rect.height,
                  borderRadius: style.borderRadius,
                  backgroundColor: style.backgroundColor,
                };
                """,
                step_button,
            )
            self.assertEqual(settings_metrics, step_metrics)
        finally:
            self.driver.set_window_size(1440, 1200)

    def test_step_editor_places_settings_action_on_validation_card(self):
        """The Tabular operation should own its settings action at the right edge.

        The operation card also carries the compact configuration summary. The
        right column should use the standard IO and signal cards instead of a
        Tabular-specific configuration card.
        """
        edit_path = reverse(
            "workflows:workflow_step_edit",
            kwargs={"pk": self.workflow.pk, "step_id": self.step.pk},
        )
        self.driver.get(f"{self.live_server_url}{edit_path}")
        operation_card = self.wait.until(
            ec.presence_of_element_located(
                (By.CSS_SELECTOR, ".validator-operation-card"),
            ),
        )
        # The settings action is now an icon-only button, so it has no
        # visible link text to match — locate it by its accessible label.
        settings_button = operation_card.find_element(
            By.CSS_SELECTOR,
            "a[aria-label='Edit settings']",
        )
        card_right = operation_card.rect["x"] + operation_card.rect["width"]
        button_right = settings_button.rect["x"] + settings_button.rect["width"]

        self.assertLess(card_right - button_right, 30)
        self.assertFalse(
            operation_card.find_elements(
                By.CSS_SELECTOR,
                ".badge.text-bg-light",
            ),
        )
        self.assertTrue(
            operation_card.find_element(
                By.CSS_SELECTOR,
                "[data-tabular-operation-summary]",
            ),
        )

        self.assertFalse(
            self.driver.find_elements(
                By.XPATH,
                (
                    "//div[contains(@class, 'card')]["
                    ".//div[contains(@class, 'card-title') "
                    "and normalize-space()='Tabular configuration']]"
                ),
            ),
        )
        input_tab = self.driver.find_element(By.ID, "signals-input-tab")
        self.assertEqual(
            input_tab.find_element(By.CSS_SELECTOR, ".badge").text,
            str(len(TABULAR_DATASET_INPUTS)),
        )
        self.assertTrue(
            self.driver.find_element(By.ID, "signals-output-tab"),
        )
        input_panel = self.driver.find_element(By.ID, "signals-input-panel")
        rendered_inputs = {
            element.text
            for element in input_panel.find_elements(By.CSS_SELECTOR, "tbody code")
        }
        self.assertEqual(
            rendered_inputs,
            {f"i.{contract_key}" for contract_key, _label in TABULAR_DATASET_INPUTS},
        )
        self.assertNotIn("No step inputs.", input_panel.text)
        self.assertTrue(
            self.driver.find_element(By.LINK_TEXT, "Edit Signals"),
        )

    def test_stage_add_buttons_use_plus_labels_and_bootstrap_tooltips(self):
        """Compact stage actions must remain clear to pointer and screen readers."""
        edit_path = reverse(
            "workflows:workflow_step_edit",
            kwargs={"pk": self.workflow.pk, "step_id": self.step.pk},
        )
        self.driver.get(f"{self.live_server_url}{edit_path}")

        stage_labels = {
            "dataset": "Add dataset assertion",
            "row": "Add row assertion",
            "column": "Add column assertion",
        }
        wrappers = {}
        for stage, label in stage_labels.items():
            button = self.wait.until(
                ec.presence_of_element_located(
                    (
                        By.CSS_SELECTOR,
                        (
                            f'[data-tabular-assertion-stage="{stage}"] '
                            f'button[aria-label="{label}"]'
                        ),
                    ),
                ),
            )
            wrapper = button.find_element(By.XPATH, "..")
            wrappers[stage] = wrapper

            visible_text = self.driver.execute_script(
                """
                const button = arguments[0];
                const plus = button.querySelector('[aria-hidden="true"]');
                const label = button.querySelector('.visually-hidden');
                const labelRect = label.getBoundingClientRect();
                return {
                  plus: plus.textContent.trim(),
                  label: label.textContent.trim(),
                  labelPosition: getComputedStyle(label).position,
                  labelWidth: labelRect.width,
                  labelHeight: labelRect.height,
                };
                """,
                button,
            )
            self.assertEqual(visible_text["plus"], "+")
            self.assertEqual(visible_text["label"], label)
            self.assertEqual(visible_text["labelPosition"], "absolute")
            self.assertLessEqual(visible_text["labelWidth"], 1)
            self.assertLessEqual(visible_text["labelHeight"], 1)
            self.assertEqual(button.get_attribute("data-bs-toggle"), "modal")
            self.assertEqual(wrapper.get_attribute("data-bs-toggle"), "tooltip")
            self.assertEqual(
                wrapper.get_attribute("data-bs-original-title"),
                label,
            )

        ActionChains(self.driver).move_to_element(wrappers["dataset"]).perform()
        tooltip = self.wait.until(
            ec.visibility_of_element_located(
                (By.CSS_SELECTOR, ".tooltip.show .tooltip-inner"),
            ),
        )
        self.assertEqual(tooltip.text.strip(), stage_labels["dataset"])

    def test_columns_start_collapsed_and_chevron_reveals_details(self):
        """Columns load collapsed; the chevron toggle reveals each column's body.

        A schema can have many columns, so showing every field at once is
        overwhelming. The page must open with each column's details accordion
        closed, and the chevron must expand it on demand.
        """
        panels = self.driver.find_elements(
            By.CSS_SELECTOR,
            ".tabular-column-card__details",
        )
        self.assertTrue(panels)
        for panel in panels:
            self.assertFalse(
                panel.is_displayed(),
                "column details should start collapsed",
            )

        first_row = self._rows()[0]
        panel = first_row.find_element(
            By.CSS_SELECTOR,
            ".tabular-column-card__details",
        )
        self._click(
            first_row.find_element(
                By.CSS_SELECTOR,
                "[data-tabular-details-toggle]",
            ),
        )
        self.wait.until(lambda _driver: panel.is_displayed())

    def test_column_controls_update_focus_order_constraints_and_keys(self):
        """Add, reorder, retag, key, and remove a column through the browser.

        This protects the complete client contract: HTMx adds a uniquely
        prefixed row and focuses it, move buttons rewrite formset order, type
        changes toggle applicable constraints, and primary keys imply Required.
        """
        initial_count = len(self._rows())
        # Use the bottom Add-column button so the new row appends to the end
        # (the top button inserts at the start).
        self._click(
            self.driver.find_element(
                By.CSS_SELECTOR,
                ".tabular-add-column-bar--bottom .tabular-add-column",
            ),
        )
        self.wait.until(lambda _driver: len(self._rows()) == initial_count + 1)
        new_row = self._rows()[-1]
        name = new_row.find_element(By.CSS_SELECTOR, 'input[name$="-name"]')
        self.assertEqual(self.driver.switch_to.active_element, name)
        name.send_keys("status")

        move_up = new_row.find_element(
            By.CSS_SELECTOR,
            '[data-tabular-move-column="up"]',
        )
        self._click(move_up)
        self.assertEqual(
            self._rows()[-2]
            .find_element(
                By.CSS_SELECTOR,
                'input[name$="-name"]',
            )
            .get_attribute("value"),
            "status",
        )

        # Name and type live on the always-visible row; everything else is in
        # the per-column "more options" accordion, so expand it before reaching
        # the flags and value constraints.
        details = new_row.find_element(
            By.CSS_SELECTOR,
            ".tabular-column-card__details",
        )
        self._click(
            new_row.find_element(
                By.CSS_SELECTOR,
                "[data-tabular-details-toggle]",
            ),
        )
        # Wait for the Bootstrap collapse to finish opening — mid-animation the
        # panel is clipped to ~0 height, which would read as "not displayed".
        self.wait.until(
            lambda _driver: (
                "show" in details.get_attribute("class")
                and "collapsing" not in details.get_attribute("class")
            ),
        )
        type_select = Select(
            new_row.find_element(By.CSS_SELECTOR, 'select[name$="-type"]'),
        )
        type_select.select_by_value("number")
        numeric_group = new_row.find_element(
            By.CSS_SELECTOR,
            '[data-tabular-constraint="numeric"]',
        )
        string_group = new_row.find_element(
            By.CSS_SELECTOR,
            '[data-tabular-constraint="string"]',
        )
        self.wait.until(lambda _driver: numeric_group.is_displayed())
        self.assertFalse(string_group.is_displayed())

        primary_key = new_row.find_element(
            By.CSS_SELECTOR,
            'input[name$="-primary_key"]',
        )
        required = new_row.find_element(
            By.CSS_SELECTOR,
            'input[name$="-required"]',
        )
        self._click(primary_key)
        self.assertTrue(required.is_selected())
        self.assertFalse(required.is_enabled())
        # While locked by the primary key, the wrapper tooltip explains why.
        required_wrapper = required.find_element(By.XPATH, "./..")
        self.assertIn(
            "Primary-key columns are always required",
            required_wrapper.get_attribute("title"),
        )
        self._click(primary_key)
        self.assertTrue(required.is_enabled())
        # Unlocked again, the tooltip reverts to the field's normal help text.
        self.assertNotIn(
            "Primary-key columns are always required",
            required_wrapper.get_attribute("title"),
        )
        self._click(required)
        self.assertFalse(required.is_selected())

        required_when_element = new_row.find_element(
            By.CSS_SELECTOR,
            'select[name$="-required_when_present"]',
        )
        required_when = Select(required_when_element)
        self.assertEqual(
            [option.get_attribute("value") for option in required_when.options],
            ["", "site_id", "reading"],
        )
        required_when.select_by_value("reading")
        self.assertEqual(
            required_when.first_selected_option.get_attribute("value"),
            "reading",
        )
        self._click(required)
        self.assertFalse(required_when_element.is_enabled())
        self.assertEqual(required_when_element.get_attribute("value"), "")
        self._click(required)
        self.assertTrue(required_when_element.is_enabled())

        self._click(
            new_row.find_element(
                By.CSS_SELECTOR,
                "[data-tabular-remove-column]",
            ),
        )
        self.assertEqual(len(self._rows()), initial_count)

    def test_import_requires_preview_before_replacing_current_columns(self):
        """Import preserves current rows until the author applies the preview."""
        # The import tool is a header-launched modal; open it, then paste the
        # descriptor and submit.
        self._click(
            self.driver.find_element(
                By.XPATH,
                "//button[contains(., 'Import Table Schema')]",
            ),
        )
        self.wait.until(
            ec.visibility_of_element_located((By.ID, "tabularImportModal")),
        )
        textarea = self.driver.find_element(By.ID, "id_table_schema")
        textarea.send_keys(
            json.dumps(
                {
                    "fields": [
                        {"name": "meter_id", "type": "string"},
                        {"name": "value", "type": "number"},
                    ],
                },
            ),
        )
        self._click(
            self.driver.find_element(
                By.XPATH,
                "//button[contains(., 'Import schema')]",
            ),
        )
        self.wait.until(
            ec.presence_of_element_located(
                (By.XPATH, "//button[contains(., 'Apply proposed schema')]"),
            ),
        )
        current_names = [
            row.find_element(
                By.CSS_SELECTOR,
                'input[name$="-name"]',
            ).get_attribute("value")
            for row in self._rows()
        ]
        self.assertEqual(current_names, ["site_id", "reading"])

        self._click(
            self.driver.find_element(
                By.XPATH,
                "//button[contains(., 'Apply proposed schema')]",
            ),
        )
        self.wait.until(
            lambda _driver: (
                [
                    row.find_element(
                        By.CSS_SELECTOR,
                        'input[name$="-name"]',
                    ).get_attribute("value")
                    for row in self._rows()
                ]
                == ["meter_id", "value"]
            ),
        )

    def test_global_stage_chooser_opens_column_cel_assistance(self):
        """The global Add flow routes to a stage-aware, assisted CEL editor.

        This covers the V2 interaction end to end: disambiguation modal, HTMx
        form load, aggregate suggestions, and canonical bracket insertion.
        """
        path = reverse(
            "workflows:workflow_step_edit",
            kwargs={"pk": self.workflow.pk, "step_id": self.step.pk},
        )
        self.driver.get(f"{self.live_server_url}{path}")
        global_add = self.wait.until(
            ec.element_to_be_clickable(
                (
                    By.CSS_SELECTOR,
                    'button[data-bs-target="#tabularAssertionStageModal"]',
                ),
            ),
        )
        self._click(global_add)
        chooser = self.wait.until(
            ec.visibility_of_element_located((By.ID, "tabularAssertionStageModal")),
        )
        self.assertIn("Which kind of assertion?", chooser.text)

        self._click(
            chooser.find_element(
                By.CSS_SELECTOR,
                '[data-tabular-stage-choice="column"]',
            ),
        )
        modal = self.wait.until(
            ec.visibility_of_element_located((By.ID, "workflowAssertionModal")),
        )
        textarea = modal.find_element(By.ID, "id_cel_expression")
        textarea.send_keys("col.reading.null")
        textarea.send_keys(Keys.CONTROL, Keys.SPACE)
        suggestion = self.wait.until(
            ec.visibility_of_element_located(
                (
                    By.XPATH,
                    "//div[@data-cel-assist-panel]//code"
                    "[contains(., 'col.reading.null_ratio')]",
                ),
            ),
        )
        self._click(suggestion.find_element(By.XPATH, ".."))
        self.assertEqual(textarea.get_attribute("value"), 'col["reading"].null_ratio')
