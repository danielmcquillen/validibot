"""Tests for deployment runtime-version settings.

``VALIDIBOT_VERSION`` is stamped by deploy recipes and consumed by backup /
restore compatibility checks. The setting has to exist on
``django.conf.settings``; merely setting the environment variable in Cloud Run
is not enough because Django only exposes values assigned by a settings module.
"""

from __future__ import annotations

from django.conf import settings
from django.test import SimpleTestCase


class RuntimeVersionSettingTests(SimpleTestCase):
    """Verify runtime-version metadata is part of the Django settings surface."""

    def test_validibot_version_setting_is_declared(self):
        """Backup manifests can read ``settings.VALIDIBOT_VERSION`` safely."""
        self.assertTrue(hasattr(settings, "VALIDIBOT_VERSION"))
        self.assertIsInstance(settings.VALIDIBOT_VERSION, str)
