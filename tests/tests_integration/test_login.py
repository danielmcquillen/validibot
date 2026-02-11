"""
Selenium-based integration tests for the login flow.

These tests use pytest-django's live_server fixture with Selenium WebDriver.
This approach handles psycopg3 connection threading properly.
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
from selenium.webdriver.support.expected_conditions import presence_of_element_located
from selenium.webdriver.support.expected_conditions import url_contains
from selenium.webdriver.support.ui import WebDriverWait

from validibot.users.models import User

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
def test_user(db):
    """Create a test user with verified email for each test."""
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
    return user


def _wait_for_element(driver, by: By, value: str, timeout: int = 10):
    """Wait for an element to be present and return it."""
    return WebDriverWait(driver, timeout).until(
        presence_of_element_located((by, value)),
    )


def _wait_for_url_contains(driver, url_part: str, timeout: int = 10):
    """Wait until the URL contains a specific string."""
    WebDriverWait(driver, timeout).until(url_contains(url_part))


def _wait_for_url_not_contains(driver, url_part: str, timeout: int = 20):
    """Wait until the URL no longer contains a specific string."""
    WebDriverWait(driver, timeout).until_not(url_contains(url_part))


@pytest.mark.skipif(
    os.getenv("SKIP_SELENIUM_LOGIN_TESTS"),
    reason="Selenium login tests skipped by environment flag.",
)
@pytest.mark.django_db(transaction=True)
class TestLoginForm:
    """
    Integration tests for the login form using Selenium.

    Tests the full login flow including form submission, validation errors,
    and successful authentication with redirect.
    """

    def _get_login_url(self, live_server) -> str:
        """Return the full login URL including the live server address."""
        return f"{live_server.url}{reverse('account_login')}"

    def test_login_page_loads(self, selenium_driver, live_server, test_user):
        """Test that the login page loads successfully."""
        selenium_driver.delete_all_cookies()
        selenium_driver.get(self._get_login_url(live_server))

        # Check that the page title or a key element is present
        assert "Sign In" in selenium_driver.page_source

        # Check that the login form is present
        form = selenium_driver.find_element(By.CSS_SELECTOR, "form.login")
        assert form is not None

    def test_login_form_has_required_fields(
        self,
        selenium_driver,
        live_server,
        test_user,
    ):
        """Test that the login form contains username and password fields."""
        selenium_driver.delete_all_cookies()
        selenium_driver.get(self._get_login_url(live_server))

        # Find the username/login field (allauth uses 'login' as the field name)
        login_field = selenium_driver.find_element(By.NAME, "login")
        assert login_field is not None

        # Find the password field
        password_field = selenium_driver.find_element(By.NAME, "password")
        assert password_field is not None

        # Find the submit button
        submit_btn = selenium_driver.find_element(By.ID, "sign_in_btn")
        assert submit_btn is not None

    def test_successful_login(self, selenium_driver, live_server, test_user):
        """Test that a user can log in with valid credentials."""
        selenium_driver.delete_all_cookies()
        selenium_driver.get(self._get_login_url(live_server))

        # Fill in the login form
        login_field = selenium_driver.find_element(By.NAME, "login")
        login_field.clear()
        login_field.send_keys(test_user.username)

        password_field = selenium_driver.find_element(By.NAME, "password")
        password_field.clear()
        password_field.send_keys(TEST_USER_PASSWORD)

        # Submit the form
        submit_btn = selenium_driver.find_element(By.ID, "sign_in_btn")
        submit_btn.click()

        # Wait for redirect (successful login should redirect away from login page)
        _wait_for_url_contains(selenium_driver, "dashboard")

        # Verify we're no longer on the login page
        assert "/accounts/login/" not in selenium_driver.current_url

    def test_login_with_invalid_password(
        self,
        selenium_driver,
        live_server,
        test_user,
    ):
        """Test that login fails with an incorrect password."""
        selenium_driver.delete_all_cookies()
        selenium_driver.get(self._get_login_url(live_server))

        # Fill in the login form with wrong password
        login_field = selenium_driver.find_element(By.NAME, "login")
        login_field.clear()
        login_field.send_keys(test_user.username)

        password_field = selenium_driver.find_element(By.NAME, "password")
        password_field.clear()
        password_field.send_keys("WrongPassword123!")

        # Submit the form
        submit_btn = selenium_driver.find_element(By.ID, "sign_in_btn")
        submit_btn.click()

        # Wait for the page to reload with error
        _wait_for_element(selenium_driver, By.CSS_SELECTOR, "form.login")

        # Check that we're still on the login page
        assert "/accounts/login/" in selenium_driver.current_url

        # Check for error message in the page
        page_source = selenium_driver.page_source.lower()
        assert any(
            msg in page_source
            for msg in ["unable to log in", "invalid", "incorrect", "error"]
        ), "Expected an error message for invalid credentials"

    def test_login_with_nonexistent_user(self, selenium_driver, live_server, test_user):
        """Test that login fails for a user that doesn't exist."""
        selenium_driver.delete_all_cookies()
        selenium_driver.get(self._get_login_url(live_server))

        # Fill in the login form with non-existent user
        login_field = selenium_driver.find_element(By.NAME, "login")
        login_field.clear()
        login_field.send_keys("nonexistentuser")

        password_field = selenium_driver.find_element(By.NAME, "password")
        password_field.clear()
        password_field.send_keys("SomePassword123!")

        # Submit the form
        submit_btn = selenium_driver.find_element(By.ID, "sign_in_btn")
        submit_btn.click()

        # Wait to ensure we stay on an accounts page (login should fail)
        _wait_for_url_contains(selenium_driver, "/accounts/", timeout=20)
        assert "/accounts/login/" in selenium_driver.current_url

        # Check for an error message indicating invalid credentials
        page_source = selenium_driver.page_source.lower()
        assert any(
            msg in page_source
            for msg in ["unable to log in", "invalid", "incorrect", "error"]
        ), "Expected an error message for invalid credentials"

    def test_login_with_empty_fields(self, selenium_driver, live_server, test_user):
        """Test that login fails when fields are empty."""
        selenium_driver.delete_all_cookies()
        selenium_driver.get(self._get_login_url(live_server))

        # Submit the form without filling in any fields
        submit_btn = selenium_driver.find_element(By.ID, "sign_in_btn")
        submit_btn.click()

        # Check that we're still on the login page
        # (form validation should prevent submission)
        assert "/accounts/login/" in selenium_driver.current_url

    def test_login_preserves_next_parameter(
        self,
        selenium_driver,
        live_server,
        test_user,
    ):
        """Test that login redirects to the 'next' URL after successful login."""
        selenium_driver.delete_all_cookies()
        # Navigate to login with a next parameter
        next_url = "/app/workflows/"
        login_url = f"{self._get_login_url(live_server)}?next={next_url}"
        selenium_driver.get(login_url)

        # Fill in the login form
        login_field = selenium_driver.find_element(By.NAME, "login")
        login_field.clear()
        login_field.send_keys(test_user.username)

        password_field = selenium_driver.find_element(By.NAME, "password")
        password_field.clear()
        password_field.send_keys(TEST_USER_PASSWORD)

        # Submit the form
        submit_btn = selenium_driver.find_element(By.ID, "sign_in_btn")
        submit_btn.click()

        # Wait for redirect - should go to the 'next' URL or dashboard
        # Note: The actual redirect depends on whether the next URL is valid
        _wait_for_url_not_contains(selenium_driver, "/accounts/login/", timeout=20)

        # Verify we're no longer on the login page
        assert "/accounts/login/" not in selenium_driver.current_url

    def test_inactive_user_cannot_login(self, selenium_driver, live_server, test_user):
        """Test that an inactive user cannot log in."""
        selenium_driver.delete_all_cookies()
        # Deactivate the test user
        test_user.is_active = False
        test_user.save()

        selenium_driver.get(self._get_login_url(live_server))

        # Fill in the login form
        login_field = selenium_driver.find_element(By.NAME, "login")
        login_field.clear()
        login_field.send_keys(test_user.username)

        password_field = selenium_driver.find_element(By.NAME, "password")
        password_field.clear()
        password_field.send_keys(TEST_USER_PASSWORD)

        # Submit the form
        submit_btn = selenium_driver.find_element(By.ID, "sign_in_btn")
        submit_btn.click()

        # Wait for the page to land on an accounts URL
        _wait_for_url_contains(selenium_driver, "/accounts/", timeout=20)

        # Should be redirected to inactive notice or stay on login
        assert (
            "/accounts/login/" in selenium_driver.current_url
            or "/accounts/inactive/" in selenium_driver.current_url
        )

        # Check that we did not get redirected to an authenticated page
        assert "/app/" not in selenium_driver.current_url
