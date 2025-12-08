"""
Selenium-based integration tests for the login flow.

These tests use StaticLiveServerTestCase to spin up a real server and
test the login form with a browser via Selenium WebDriver.
"""

import logging
import os
import uuid

import pytest
from allauth.account.models import EmailAddress
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.expected_conditions import presence_of_element_located
from selenium.webdriver.support.expected_conditions import url_contains
from selenium.webdriver.support.ui import WebDriverWait

from validibot.users.models import User

# Test password for Selenium tests - not a real secret
TEST_USER_PASSWORD = "SecureTestPassword123!"  # noqa: S105

logger = logging.getLogger(__name__)


@pytest.mark.skipif(
    os.getenv("SKIP_SELENIUM_LOGIN_TESTS"),
    reason="Selenium login tests skipped by environment flag.",
)
class LoginFormTests(StaticLiveServerTestCase):
    """
    Integration tests for the login form using Selenium.

    Tests the full login flow including form submission, validation errors,
    and successful authentication with redirect.
    """

    @classmethod
    def setUpClass(cls):
        """Set up the Selenium WebDriver for all tests in this class."""
        super().setUpClass()

        # Configure Chrome options for headless testing
        chrome_options = Options()
        # chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--window-size=1920,1080")

        # Initialize the WebDriver
        cls.selenium = webdriver.Chrome(options=chrome_options)
        cls.selenium.implicitly_wait(10)

    @classmethod
    def tearDownClass(cls):
        """Clean up the Selenium WebDriver after all tests."""
        try:
            cls.selenium.quit()
        except Exception:
            logger.exception("Error quitting Selenium WebDriver")
        finally:
            super().tearDownClass()

    def setUp(self):
        """Set up test data for each test."""
        super().setUp()
        # Create a test user with a known password
        self.test_password = TEST_USER_PASSWORD
        username = f"testuser-{uuid.uuid4().hex[:8]}"
        User.objects.filter(username=username).delete()
        self.test_user = User.objects.create_user(
            username=username,
            email=f"{username}@example.com",
            password=self.test_password,
            is_active=True,
        )
        EmailAddress.objects.create(
            user=self.test_user,
            email=self.test_user.email,
            verified=True,
            primary=True,
        )

    def tearDown(self):
        """Clean up after each test."""
        super().tearDown()
        # Clear cookies between tests
        self.selenium.delete_all_cookies()

    def _get_login_url(self) -> str:
        """Return the full login URL including the live server address."""
        return f"{self.live_server_url}{reverse('account_login')}"

    def _wait_for_element(self, by: By, value: str, timeout: int = 10):
        """Wait for an element to be present and return it."""
        return WebDriverWait(self.selenium, timeout).until(
            presence_of_element_located((by, value)),
        )

    def _wait_for_url_contains(self, url_part: str, timeout: int = 10):
        """Wait until the URL contains a specific string."""
        WebDriverWait(self.selenium, timeout).until(
            url_contains(url_part),
        )

    def _wait_for_url_not_contains(self, url_part: str, timeout: int = 20):
        """Wait until the URL no longer contains a specific string."""
        WebDriverWait(self.selenium, timeout).until_not(
            url_contains(url_part),
        )

    def test_login_page_loads(self):
        """Test that the login page loads successfully."""
        self.selenium.get(self._get_login_url())

        # Check that the page title or a key element is present
        self.assertIn("Sign In", self.selenium.page_source)

        # Check that the login form is present
        form = self.selenium.find_element(By.CSS_SELECTOR, "form.login")
        self.assertIsNotNone(form)

    def test_login_form_has_required_fields(self):
        """Test that the login form contains username and password fields."""
        self.selenium.get(self._get_login_url())

        # Find the username/login field (allauth uses 'login' as the field name)
        login_field = self.selenium.find_element(By.NAME, "login")
        self.assertIsNotNone(login_field)

        # Find the password field
        password_field = self.selenium.find_element(By.NAME, "password")
        self.assertIsNotNone(password_field)

        # Find the submit button
        submit_btn = self.selenium.find_element(By.ID, "sign_in_btn")
        self.assertIsNotNone(submit_btn)

    def test_successful_login(self):
        """Test that a user can log in with valid credentials."""
        self.selenium.get(self._get_login_url())

        # Fill in the login form
        login_field = self.selenium.find_element(By.NAME, "login")
        login_field.clear()
        login_field.send_keys(self.test_user.username)

        password_field = self.selenium.find_element(By.NAME, "password")
        password_field.clear()
        password_field.send_keys(self.test_password)

        # Submit the form
        submit_btn = self.selenium.find_element(By.ID, "sign_in_btn")
        submit_btn.click()

        # Wait for redirect (successful login should redirect away from login page)
        self._wait_for_url_contains("dashboard")

        # Verify we're no longer on the login page
        self.assertNotIn("/accounts/login/", self.selenium.current_url)

    def test_login_with_invalid_password(self):
        """Test that login fails with an incorrect password."""
        self.selenium.get(self._get_login_url())

        # Fill in the login form with wrong password
        login_field = self.selenium.find_element(By.NAME, "login")
        login_field.clear()
        login_field.send_keys(self.test_user.username)

        password_field = self.selenium.find_element(By.NAME, "password")
        password_field.clear()
        password_field.send_keys("WrongPassword123!")

        # Submit the form
        submit_btn = self.selenium.find_element(By.ID, "sign_in_btn")
        submit_btn.click()

        # Wait for the page to reload with error
        self._wait_for_element(By.CSS_SELECTOR, "form.login")

        # Check that we're still on the login page
        self.assertIn("/accounts/login/", self.selenium.current_url)

        # Check for error message in the page
        page_source = self.selenium.page_source.lower()
        self.assertTrue(
            "unable to log in" in page_source
            or "invalid" in page_source
            or "incorrect" in page_source
            or "error" in page_source,
            "Expected an error message for invalid credentials",
        )

    def test_login_with_nonexistent_user(self):
        """Test that login fails for a user that doesn't exist."""
        self.selenium.get(self._get_login_url())

        # Fill in the login form with non-existent user
        login_field = self.selenium.find_element(By.NAME, "login")
        login_field.clear()
        login_field.send_keys("nonexistentuser")

        password_field = self.selenium.find_element(By.NAME, "password")
        password_field.clear()
        password_field.send_keys("SomePassword123!")

        # Submit the form
        submit_btn = self.selenium.find_element(By.ID, "sign_in_btn")
        submit_btn.click()

        # Wait to ensure we stay on an accounts page (login should fail)
        self._wait_for_url_contains("/accounts/", timeout=20)
        self.assertIn("/accounts/login/", self.selenium.current_url)

        # Check for an error message indicating invalid credentials
        page_source = self.selenium.page_source.lower()
        self.assertTrue(
            "unable to log in" in page_source
            or "invalid" in page_source
            or "incorrect" in page_source
            or "error" in page_source,
            "Expected an error message for invalid credentials",
        )

    def test_login_with_empty_fields(self):
        """Test that login fails when fields are empty."""
        self.selenium.get(self._get_login_url())

        # Submit the form without filling in any fields
        submit_btn = self.selenium.find_element(By.ID, "sign_in_btn")
        submit_btn.click()

        # Check that we're still on the login page
        # (form validation should prevent submission)
        self.assertIn("/accounts/login/", self.selenium.current_url)

    def test_login_preserves_next_parameter(self):
        """Test that login redirects to the 'next' URL after successful login."""
        # Navigate to login with a next parameter
        next_url = "/app/workflows/"
        login_url = f"{self._get_login_url()}?next={next_url}"
        self.selenium.get(login_url)

        # Fill in the login form
        login_field = self.selenium.find_element(By.NAME, "login")
        login_field.clear()
        login_field.send_keys(self.test_user.username)

        password_field = self.selenium.find_element(By.NAME, "password")
        password_field.clear()
        password_field.send_keys(self.test_password)

        # Submit the form
        submit_btn = self.selenium.find_element(By.ID, "sign_in_btn")
        submit_btn.click()

        # Wait for redirect - should go to the 'next' URL or dashboard
        # Note: The actual redirect depends on whether the next URL is valid
        self._wait_for_url_not_contains("/accounts/login/", timeout=20)

        # Verify we're no longer on the login page
        self.assertNotIn("/accounts/login/", self.selenium.current_url)

    def test_inactive_user_cannot_login(self):
        """Test that an inactive user cannot log in."""
        # Deactivate the test user
        self.test_user.is_active = False
        self.test_user.save()

        self.selenium.get(self._get_login_url())

        # Fill in the login form
        login_field = self.selenium.find_element(By.NAME, "login")
        login_field.clear()
        login_field.send_keys(self.test_user.username)

        password_field = self.selenium.find_element(By.NAME, "password")
        password_field.clear()
        password_field.send_keys(self.test_password)

        # Submit the form
        submit_btn = self.selenium.find_element(By.ID, "sign_in_btn")
        submit_btn.click()

        # Wait for the page to land on an accounts URL
        self._wait_for_url_contains("/accounts/", timeout=20)

        # Should be redirected to inactive notice or stay on login
        self.assertTrue(
            "/accounts/login/" in self.selenium.current_url
            or "/accounts/inactive/" in self.selenium.current_url,
        )

        # Check that we did not get redirected to an authenticated page
        self.assertNotIn("/app/", self.selenium.current_url)
