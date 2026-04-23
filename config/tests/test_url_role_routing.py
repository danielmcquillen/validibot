"""
Ensure URL routing respects APP_ROLE.

Web role should expose UI/public API but not internal callback.
Worker role should expose internal callback but not UI routes.
"""

from __future__ import annotations

import importlib

import pytest
from django.test import SimpleTestCase
from django.test import override_settings
from django.urls import Resolver404
from django.urls import clear_url_caches
from django.urls import resolve


class UrlRoleRoutingTests(SimpleTestCase):
    def _reload_urls(self):
        # ``config.urls`` branches to ``config.urls_web`` /
        # ``config.urls_worker`` based on APP_ROLE, and those modules
        # snapshot the role at import time. Reload the chain so
        # override_settings(APP_ROLE=...) actually takes effect.
        import config.urls
        import config.urls_web
        import config.urls_worker

        clear_url_caches()
        importlib.reload(config.urls_web)
        importlib.reload(config.urls_worker)
        importlib.reload(config.urls)

    @override_settings(APP_ROLE="web", APP_IS_WORKER=False, ENABLE_API=True)
    def test_web_role_has_public_api_and_ui(self):
        self._reload_urls()
        # UI route should resolve. The root path is mounted from
        # ``validibot.home.urls`` with namespace ``home`` in
        # config.urls_web — the older assertion pointed at a
        # non-existent ``marketing`` namespace that never lived in this
        # (community) repo.
        self.assertEqual(resolve("/").namespace, "home")
        # Public API route should resolve. We hit ``/api/v1/auth/me/``
        # because that's a stable non-kwarg route under the ``api``
        # namespace; the older ``/api/v1/workflows/`` assertion broke
        # when workflows became org-scoped
        # (``/api/v1/orgs/<org_slug>/workflows/``).
        self.assertEqual(resolve("/api/v1/auth/me/").namespace, "api")
        # Internal callback should 404
        with pytest.raises(Resolver404):
            resolve("/api/v1/validation-callbacks/")

    @override_settings(APP_ROLE="worker", APP_IS_WORKER=True, ENABLE_API=True)
    def test_worker_role_has_internal_callback_only(self):
        self._reload_urls()
        # Internal callback should resolve
        match = resolve("/api/v1/validation-callbacks/")
        self.assertEqual(match.url_name, "validation-callbacks")
        # Tracking-event receiver should resolve too. This is the
        # Cloud Tasks target for CloudTasksTrackingDispatcher;
        # dropping it from worker URLs would silently break login
        # tracking on GCP.
        match = resolve("/api/v1/tasks/tracking/log-event/")
        self.assertEqual(match.url_name, "tasks-tracking-log-event")
        # UI route should 404
        with pytest.raises(Resolver404):
            resolve("/")
