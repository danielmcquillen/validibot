"""
Tests for security headers and hardening measures.

Covers:
- Content-Security-Policy header is present and contains expected directives
- Referrer-Policy header is set correctly
- Permissions-Policy header disables unused browser APIs
- Download view sanitizes user-supplied filenames

These tests verify the server-side contract (headers are set correctly).
Browser-side CSP enforcement is not testable in Django's test client.
"""

from __future__ import annotations

from http import HTTPStatus

import pytest
from django.test import override_settings
from django.urls import reverse

from validibot.core.storage.local import LocalDataStorage


@pytest.fixture
def authenticated_client(client):
    """Create and log in a test user, returning the client."""
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(
        username="sectest",
        email="sectest@example.com",
        password="StrongPass!23",  # noqa: S106
    )
    client.force_login(user)
    return client


# ---------------------------------------------------------------------------
# CSP enforcing header
# ---------------------------------------------------------------------------


class TestCSPHeaders:
    """Verify that the Content-Security-Policy header is present.

    django-csp adds this header via middleware. These tests confirm the
    middleware is active and the policy contains expected directives.

    The CSP is in enforcing mode (Content-Security-Policy header, not
    Content-Security-Policy-Report-Only). Switched from report-only to
    enforcing on 2026-03-17 after verifying no violations on all pages.
    """

    CSP_HEADER = "Content-Security-Policy"

    @pytest.mark.django_db
    def test_csp_header_present(self, authenticated_client):
        """CSP header should be set on authenticated page responses."""
        response = authenticated_client.get(reverse("home:home"))
        csp = response.get(self.CSP_HEADER, "")
        assert csp, "CSP header should be present"

    @pytest.mark.django_db
    def test_csp_includes_default_src_self(self, authenticated_client):
        """CSP policy should include default-src 'self'."""
        response = authenticated_client.get(reverse("home:home"))
        csp = response.get(self.CSP_HEADER, "")
        assert "default-src 'self'" in csp

    @pytest.mark.django_db
    def test_csp_includes_script_src(self, authenticated_client):
        """CSP policy should include script-src 'self'."""
        response = authenticated_client.get(reverse("home:home"))
        csp = response.get(self.CSP_HEADER, "")
        assert "script-src 'self'" in csp

    @pytest.mark.django_db
    def test_csp_includes_google_fonts(self, authenticated_client):
        """CSP style-src should allow Google Fonts."""
        response = authenticated_client.get(reverse("home:home"))
        csp = response.get(self.CSP_HEADER, "")
        assert "fonts.googleapis.com" in csp

    @pytest.mark.django_db
    def test_csp_allows_posthog_connect(self, authenticated_client):
        """CSP connect-src should allow PostHog analytics."""
        response = authenticated_client.get(reverse("home:home"))
        csp = response.get(self.CSP_HEADER, "")
        assert "us.i.posthog.com" in csp


# ---------------------------------------------------------------------------
# Referrer-Policy header
# ---------------------------------------------------------------------------


class TestReferrerPolicy:
    """Verify Referrer-Policy header prevents URL parameter leakage.

    Django's SecurityMiddleware sets this header based on the
    SECURE_REFERRER_POLICY setting. Without it, browsers may send full
    URLs (including tokens and IDs) in the Referer header to external sites.
    """

    @pytest.mark.django_db
    def test_referrer_policy_header_present(self, authenticated_client):
        """Referrer-Policy should be set to strict-origin-when-cross-origin."""
        response = authenticated_client.get(reverse("home:home"))
        assert response.get("Referrer-Policy") == "strict-origin-when-cross-origin"


# ---------------------------------------------------------------------------
# Permissions-Policy header
# ---------------------------------------------------------------------------


class TestPermissionsPolicy:
    """Verify Permissions-Policy header disables unused browser APIs.

    This is defense-in-depth: even if an attacker achieves XSS, they
    cannot access the camera, microphone, geolocation, etc.
    """

    @pytest.mark.django_db
    def test_permissions_policy_header_present(self, authenticated_client):
        """Permissions-Policy header should be present and disable APIs."""
        response = authenticated_client.get(reverse("home:home"))
        pp = response.get("Permissions-Policy", "")
        assert pp, "Permissions-Policy header should be present"
        assert "camera=()" in pp
        assert "microphone=()" in pp
        assert "geolocation=()" in pp


# ---------------------------------------------------------------------------
# Download view filename sanitization
# ---------------------------------------------------------------------------


class TestDownloadFilenameSanitization:
    """Verify the download view sanitizes user-supplied filenames.

    The data_download view accepts an optional 'filename' GET parameter.
    Previously this was passed directly to FileResponse, allowing potential
    header injection or path confusion. Now it's sanitized via
    sanitize_filename() before use.
    """

    @pytest.fixture
    def download_setup(self, authenticated_client, tmp_path):
        """Create a temporary file and sign a download token for it."""
        # Create a file to serve
        test_file = tmp_path / "test_data.json"
        test_file.write_text('{"key": "value"}')

        # Configure local storage to use the temp dir
        LocalDataStorage(root=str(tmp_path))
        # The file path relative to the storage root
        rel_path = "test_data.json"

        # Sign the path for download
        token = LocalDataStorage.sign_download(rel_path, max_age=300)

        return {
            "client": authenticated_client,
            "token": token,
            "file_path": test_file,
            "storage_root": str(tmp_path),
        }

    @pytest.mark.django_db
    def test_clean_filename_passes_through(self, download_setup):
        """A normal filename should pass through sanitization unchanged."""
        setup = download_setup
        url = reverse("core:data_download")
        with override_settings(
            DATA_STORAGE_BACKEND="local",
            DATA_STORAGE_ROOT=setup["storage_root"],
            DATA_STORAGE_OPTIONS={"root": setup["storage_root"]},
        ):
            response = setup["client"].get(
                url,
                {"token": setup["token"], "filename": "report.json"},
            )
        assert response.status_code == HTTPStatus.OK
        content_disposition = response.get("Content-Disposition", "")
        assert "report.json" in content_disposition

    @pytest.mark.django_db
    def test_path_traversal_filename_sanitized(self, download_setup):
        """Path traversal characters should be stripped from the filename."""
        setup = download_setup
        url = reverse("core:data_download")
        with override_settings(
            DATA_STORAGE_BACKEND="local",
            DATA_STORAGE_ROOT=setup["storage_root"],
            DATA_STORAGE_OPTIONS={"root": setup["storage_root"]},
        ):
            response = setup["client"].get(
                url,
                {
                    "token": setup["token"],
                    "filename": "../../etc/passwd",
                },
            )
        assert response.status_code == HTTPStatus.OK
        content_disposition = response.get("Content-Disposition", "")
        # Path components should be stripped, leaving just "passwd"
        assert "../../" not in content_disposition
        assert "passwd" in content_disposition

    @pytest.mark.django_db
    def test_xss_filename_sanitized(self, download_setup):
        """Script injection in filename should be neutralized."""
        setup = download_setup
        url = reverse("core:data_download")
        with override_settings(
            DATA_STORAGE_BACKEND="local",
            DATA_STORAGE_ROOT=setup["storage_root"],
            DATA_STORAGE_OPTIONS={"root": setup["storage_root"]},
        ):
            response = setup["client"].get(
                url,
                {
                    "token": setup["token"],
                    "filename": '<script>alert("xss")</script>.json',
                },
            )
        assert response.status_code == HTTPStatus.OK
        content_disposition = response.get("Content-Disposition", "")
        assert "<script>" not in content_disposition
