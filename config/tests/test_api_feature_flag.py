from importlib import reload

import pytest
from django.test import SimpleTestCase
from django.urls import NoReverseMatch
from django.urls import Resolver404
from django.urls import clear_url_caches
from django.urls import resolve
from django.urls import reverse
from django.urls import set_urlconf


class ApiFeatureFlagTests(SimpleTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._reload_urls()

    def _reload_urls(self) -> None:
        # ``config.urls`` delegates to ``config.urls_web`` for web-role
        # deployments, and ``urls_web`` is where the ``ENABLE_API`` flag
        # actually gates the api_router include. Reloading only
        # ``config.urls`` leaves the cached ``urls_web`` module intact,
        # so the gated routes keep being reachable. Reload the chain.
        import config.urls as config_urls
        import config.urls_web as config_urls_web

        reload(config_urls_web)
        reload(config_urls)
        clear_url_caches()
        set_urlconf(None)

    def test_api_urls_enabled_by_default(self) -> None:
        # ``api:auth-me`` is a simple non-kwarg route registered directly
        # in config.api_router — a stable canary that the API URLConf is
        # mounted when ENABLE_API is on. The older assertion used
        # ``api:workflow-list`` but workflows are now org-scoped
        # (``api:org-workflows-list`` requires an ``org_slug`` kwarg).
        url = reverse("api:auth-me")
        self.assertEqual(url, "/api/v1/auth/me/")
        match = resolve(url)
        self.assertIsNotNone(match.func)

    def test_api_urls_disabled_when_flag_false(self) -> None:
        with self.settings(ENABLE_API=False):
            self._reload_urls()
            with pytest.raises(NoReverseMatch):
                reverse("api:auth-me")

            with pytest.raises(Resolver404):
                resolve("/api/v1/auth/me/")

            with pytest.raises(NoReverseMatch):
                reverse("obtain_auth_token")

            with pytest.raises(Resolver404):
                resolve("/api/v1/auth-token/")

        self._reload_urls()
