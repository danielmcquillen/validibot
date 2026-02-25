"""
Selenium-based integration tests for the signal detail UI.

These tests verify that:
1. Signal info buttons open modals correctly
2. Modal content displays signal information
3. Modals can be closed properly
4. View all signals link navigates correctly
"""

import logging
import os
import uuid
from pathlib import Path

import pytest
from allauth.account.models import EmailAddress
from django.urls import reverse
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.ui import WebDriverWait

from validibot.users.constants import RoleCode
from validibot.users.models import User
from validibot.users.tests.factories import MembershipFactory
from validibot.users.tests.factories import OrganizationFactory
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import ValidationType
from validibot.validations.tests.factories import ValidatorCatalogEntryFactory
from validibot.validations.tests.factories import ValidatorFactory

# Test password for Selenium tests - not a real secret
TEST_USER_PASSWORD = "SecureTestPassword123!"  # noqa: S105

logger = logging.getLogger(__name__)


def _first_existing_path(*candidates: str) -> str | None:
    """Return the first existing path from the provided candidates."""
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def _get_chrome_options() -> Options:
    """Configure Chrome options for headless testing."""
    chrome_options = Options()
    use_headless = os.getenv("SELENIUM_HEADLESS", "1") != "0"
    if use_headless:
        chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--window-size=1920,1080")

    chrome_binary = _first_existing_path(
        os.getenv("CHROME_BIN"),
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    )
    if chrome_binary:
        chrome_options.binary_location = chrome_binary

    return chrome_options


def _get_chromedriver_path() -> str:
    """Get the path to chromedriver, raising if not found."""
    chromedriver_path = _first_existing_path(
        os.getenv("CHROMEDRIVER_PATH"),
        "/usr/bin/chromedriver",
        "/usr/lib/chromium/chromedriver",
    )
    if not chromedriver_path:
        raise RuntimeError(
            "Chromedriver not found. Run integration tests via "
            "`just test-integration` to use the container with Chrome "
            "preinstalled, or set CHROMEDRIVER_PATH to a valid binary.",
        )
    return chromedriver_path


@pytest.fixture(scope="module")
def selenium_driver():
    """Create a Selenium WebDriver for the test module."""
    driver = webdriver.Chrome(
        service=Service(executable_path=_get_chromedriver_path()),
        options=_get_chrome_options(),
    )
    driver.implicitly_wait(10)
    yield driver
    try:
        driver.quit()
    except Exception:
        logger.exception("Error quitting Selenium WebDriver")


@pytest.fixture
def test_user_with_org(db):
    """Create a test user with org and admin membership."""
    username = f"testuser-{uuid.uuid4().hex[:8]}"
    user = User.objects.create_user(
        username=username,
        email=f"{username}@example.com",
        password=TEST_USER_PASSWORD,
        is_active=True,
    )
    EmailAddress.objects.create(
        user=user,
        email=user.email,
        verified=True,
        primary=True,
    )
    org = OrganizationFactory()
    membership = MembershipFactory(user=user, org=org)
    membership.add_role(RoleCode.ADMIN)
    user.set_current_org(org)
    return user, org


@pytest.fixture
def validator_with_signals(test_user_with_org):
    """Create a validator with input and output signals."""
    user, org = test_user_with_org
    validator = ValidatorFactory(
        name="Selenium Test Validator",
        slug="selenium-test-validator",
        validation_type=ValidationType.ENERGYPLUS,
        is_system=True,
        has_processor=True,
    )
    input_signal = ValidatorCatalogEntryFactory(
        validator=validator,
        slug="floor_area_m2",
        label="Floor Area (m2)",
        run_stage=CatalogRunStage.INPUT,
        data_type=CatalogValueType.NUMBER,
        description="Total floor area in square meters",
        target_data_path="inputs.floor_area",
    )
    output_signal = ValidatorCatalogEntryFactory(
        validator=validator,
        slug="energy_consumption_kwh",
        label="Energy Consumption (kWh)",
        run_stage=CatalogRunStage.OUTPUT,
        data_type=CatalogValueType.NUMBER,
        description="Total energy consumption in kilowatt hours",
        target_data_path="outputs.energy",
    )
    return validator, input_signal, output_signal


def _login_user(driver, live_server, user):
    """Log in a user via the login form."""
    login_url = f"{live_server.url}{reverse('account_login')}"
    driver.delete_all_cookies()
    driver.get(login_url)

    login_field = driver.find_element(By.NAME, "login")
    login_field.clear()
    login_field.send_keys(user.username)

    password_field = driver.find_element(By.NAME, "password")
    password_field.clear()
    password_field.send_keys(TEST_USER_PASSWORD)

    submit_btn = driver.find_element(By.ID, "sign_in_btn")
    submit_btn.click()

    # Wait for login to complete
    WebDriverWait(driver, 10).until_not(
        expected_conditions.url_contains("/accounts/login/"),
    )


def _wait_for_element(driver, by: By, value: str, timeout: int = 10):
    """Wait for an element to be present and return it."""
    return WebDriverWait(driver, timeout).until(
        expected_conditions.presence_of_element_located((by, value)),
    )


def _wait_for_element_visible(driver, by: By, value: str, timeout: int = 10):
    """Wait for an element to be visible and return it."""
    return WebDriverWait(driver, timeout).until(
        expected_conditions.visibility_of_element_located((by, value)),
    )


def _wait_for_element_clickable(driver, by: By, value: str, timeout: int = 10):
    """Wait for an element to be clickable and return it."""
    return WebDriverWait(driver, timeout).until(
        expected_conditions.element_to_be_clickable((by, value)),
    )


@pytest.mark.skipif(
    os.getenv("SKIP_SELENIUM_TESTS"),
    reason="Selenium tests skipped by environment flag.",
)
@pytest.mark.django_db(transaction=True)
class TestSignalDetailModal:
    """Integration tests for signal detail modals using Selenium."""

    def test_signal_info_button_opens_modal(
        self,
        selenium_driver,
        live_server,
        test_user_with_org,
        validator_with_signals,
    ):
        """Test that clicking the info button opens the signal detail modal."""
        user, org = test_user_with_org
        validator, input_signal, output_signal = validator_with_signals

        # Login
        _login_user(selenium_driver, live_server, user)

        # Navigate to validator detail page
        detail_path = reverse(
            "validations:validator_detail",
            kwargs={"slug": validator.slug},
        )
        validator_url = f"{live_server.url}{detail_path}"
        selenium_driver.get(validator_url)

        # Wait for page to load and find the info button for input signal
        info_button_selector = (
            f'button[data-bs-target="#modal-signal-detail-{input_signal.id}"]'
        )
        info_button = _wait_for_element_clickable(
            selenium_driver,
            By.CSS_SELECTOR,
            info_button_selector,
        )

        # Click the info button
        info_button.click()

        # Wait for modal to be visible
        modal_selector = f"#modal-signal-detail-{input_signal.id}"
        modal = _wait_for_element_visible(
            selenium_driver,
            By.CSS_SELECTOR,
            modal_selector,
        )

        # Verify modal is displayed
        assert modal.is_displayed()

        # Verify modal contains signal information
        modal_body = modal.find_element(By.CSS_SELECTOR, ".modal-body")
        modal_text = modal_body.text

        assert input_signal.slug in modal_text
        assert "Floor Area (m2)" in modal_text or input_signal.label in modal_text
        assert "Input" in modal_text
        assert "Number" in modal_text

    def test_signal_modal_can_be_closed(
        self,
        selenium_driver,
        live_server,
        test_user_with_org,
        validator_with_signals,
    ):
        """Test that the signal detail modal can be closed."""
        user, org = test_user_with_org
        validator, input_signal, output_signal = validator_with_signals

        # Login
        _login_user(selenium_driver, live_server, user)

        # Navigate to validator detail page
        detail_path = reverse(
            "validations:validator_detail",
            kwargs={"slug": validator.slug},
        )
        validator_url = f"{live_server.url}{detail_path}"
        selenium_driver.get(validator_url)

        # Open the modal
        info_button_selector = (
            f'button[data-bs-target="#modal-signal-detail-{input_signal.id}"]'
        )
        info_button = _wait_for_element_clickable(
            selenium_driver,
            By.CSS_SELECTOR,
            info_button_selector,
        )
        info_button.click()

        # Wait for modal to be visible
        modal_selector = f"#modal-signal-detail-{input_signal.id}"
        modal = _wait_for_element_visible(
            selenium_driver,
            By.CSS_SELECTOR,
            modal_selector,
        )

        # Find and click the close button
        close_button = modal.find_element(
            By.CSS_SELECTOR,
            '.modal-footer button[data-bs-dismiss="modal"]',
        )
        close_button.click()

        # Wait for modal to be hidden
        WebDriverWait(selenium_driver, 5).until(
            expected_conditions.invisibility_of_element_located(
                (By.CSS_SELECTOR, modal_selector),
            ),
        )

    def test_view_all_signals_link_navigates_correctly(
        self,
        selenium_driver,
        live_server,
        test_user_with_org,
        validator_with_signals,
    ):
        """Test that 'View all' link navigates to signals list page."""
        user, org = test_user_with_org
        validator, input_signal, output_signal = validator_with_signals

        # Login
        _login_user(selenium_driver, live_server, user)

        # Navigate to validator detail page
        detail_path = reverse(
            "validations:validator_detail",
            kwargs={"slug": validator.slug},
        )
        validator_url = f"{live_server.url}{detail_path}"
        selenium_driver.get(validator_url)

        # Find and click the "View all" link
        view_all_link = _wait_for_element_clickable(
            selenium_driver,
            By.CSS_SELECTOR,
            'a[href*="/signals/"]',
        )
        view_all_link.click()

        # Wait for navigation to signals list page
        WebDriverWait(selenium_driver, 10).until(
            expected_conditions.url_contains("/signals/"),
        )

        # Verify we're on the signals list page
        assert "/signals/" in selenium_driver.current_url

        # Verify page content
        page_source = selenium_driver.page_source
        assert "Signals for" in page_source
        assert validator.name in page_source
        assert input_signal.slug in page_source
        assert output_signal.slug in page_source

    def test_signals_list_back_button_navigates_correctly(
        self,
        selenium_driver,
        live_server,
        test_user_with_org,
        validator_with_signals,
    ):
        """Test that back button on signals list navigates to validator detail."""
        user, org = test_user_with_org
        validator, input_signal, output_signal = validator_with_signals

        # Login
        _login_user(selenium_driver, live_server, user)

        # Navigate directly to signals list page
        signals_path = reverse(
            "validations:validator_signals_list",
            kwargs={"slug": validator.slug},
        )
        signals_url = f"{live_server.url}{signals_path}"
        selenium_driver.get(signals_url)

        # Wait for page to load
        _wait_for_element(selenium_driver, By.CSS_SELECTOR, ".bi-arrow-left")

        # Find and click the back button
        back_button = selenium_driver.find_element(
            By.CSS_SELECTOR,
            "a.btn-secondary .bi-arrow-left",
        ).find_element(By.XPATH, "./..")
        back_button.click()

        # Wait for navigation
        WebDriverWait(selenium_driver, 10).until_not(
            expected_conditions.url_contains("/signals/"),
        )

        # Verify we're back on the validator detail page
        assert f"/library/custom/{validator.slug}/" in selenium_driver.current_url
        assert "/signals/" not in selenium_driver.current_url

    def test_no_template_comments_in_page(
        self,
        selenium_driver,
        live_server,
        test_user_with_org,
        validator_with_signals,
    ):
        """Test that no Django template comments appear in the rendered page."""
        user, org = test_user_with_org
        validator, input_signal, output_signal = validator_with_signals

        # Login
        _login_user(selenium_driver, live_server, user)

        # Navigate to validator detail page
        detail_path = reverse(
            "validations:validator_detail",
            kwargs={"slug": validator.slug},
        )
        validator_url = f"{live_server.url}{detail_path}"
        selenium_driver.get(validator_url)

        # Wait for page to fully load
        _wait_for_element(selenium_driver, By.CSS_SELECTOR, ".card")

        # Get full page source
        page_source = selenium_driver.page_source

        # Verify no template comment syntax
        assert "{#" not in page_source, "Template comment start found in page"
        assert "#}" not in page_source, "Template comment end found in page"
        assert "Context required:" not in page_source, "Docstring content leaked"
        assert "ValidatorCatalogEntry instance" not in page_source, "Docstring leaked"
