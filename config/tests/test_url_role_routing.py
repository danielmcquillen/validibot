"""
Ensure URL routing respects APP_ROLE.

Web role should expose UI/public API but not internal callback.
Worker role should expose internal callback but not UI routes.
"""

from __future__ import annotations

import importlib
from django.conf import settings
from django.test import SimpleTestCase, override_settings
from django.urls import resolve, Resolver404, clear_url_caches


class UrlRoleRoutingTests(SimpleTestCase):
    def _reload_urls(self):
        import config.urls
        clear_url_caches()
        importlib.reload(config.urls)

    @override_settings(APP_ROLE="web", APP_IS_WORKER=False, ENABLE_API=True)
    def test_web_role_has_public_api_and_ui(self):
        self._reload_urls()
        # UI route should resolve
        self.assertEqual(resolve("/").namespace, "marketing")
        # Public API route should resolve
        self.assertEqual(resolve("/api/v1/workflows/").namespace, "api")
        # Internal callback should 404
        with self.assertRaises(Resolver404):
            resolve("/api/v1/validation-callbacks/")

    @override_settings(APP_ROLE="worker", APP_IS_WORKER=True, ENABLE_API=True)
    def test_worker_role_has_internal_callback_only(self):
        self._reload_urls()
        # Internal callback should resolve
        match = resolve("/api/v1/validation-callbacks/")
        self.assertEqual(match.url_name, "validation-callbacks")
        # UI route should 404
        with self.assertRaises(Resolver404):
            resolve("/")
