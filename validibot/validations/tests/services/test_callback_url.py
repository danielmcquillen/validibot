"""
Tests for worker callback URL construction.

These tests ensure validator callbacks target the worker service base URL
(`WORKER_URL`) rather than the public site base URL (`SITE_URL`).
"""

import pytest
from django.test import override_settings

from validibot.validations.services.cloud_run.launcher import (
    build_validation_callback_url,
)


def test_build_validation_callback_url_uses_worker_url():
    """Uses `WORKER_URL` (not `SITE_URL`) when it is provided."""
    with override_settings(
        WORKER_URL="https://validibot-worker.example.a.run.app/",
        SITE_URL="https://validibot.example.com/",
    ):
        assert (
            build_validation_callback_url()
            == "https://validibot-worker.example.a.run.app/api/v1/validation-callbacks/"
        )


def test_build_validation_callback_url_falls_back_to_site_url(caplog):
    """Falls back to `SITE_URL` with a warning when `WORKER_URL` is unset."""
    caplog.set_level("WARNING")
    with override_settings(
        WORKER_URL="",
        SITE_URL="https://validibot-web.example.a.run.app",
    ):
        assert (
            build_validation_callback_url()
            == "https://validibot-web.example.a.run.app/api/v1/validation-callbacks/"
        )
        assert "WORKER_URL is not set; falling back to SITE_URL" in caplog.text


def test_build_validation_callback_url_requires_a_base_url():
    """Raises a clear error when neither `WORKER_URL` nor `SITE_URL` is set."""
    with (
        override_settings(WORKER_URL="", SITE_URL=""),
        pytest.raises(ValueError, match="WORKER_URL"),
    ):
        build_validation_callback_url()
