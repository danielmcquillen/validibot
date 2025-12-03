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
        from config import urls as config_urls

        reload(config_urls)
        clear_url_caches()
        set_urlconf(None)

    def test_api_urls_enabled_by_default(self) -> None:
        url = reverse("api:workflow-list")
        self.assertEqual(url, "/api/v1/workflows/")
        match = resolve(url)
        self.assertIsNotNone(match.func)

    def test_api_urls_disabled_when_flag_false(self) -> None:
        with self.settings(ENABLE_API=False):
            self._reload_urls()
            with pytest.raises(NoReverseMatch):
                reverse("api:workflow-list")

            with pytest.raises(Resolver404):
                resolve("/api/v1/workflows/")

            with pytest.raises(NoReverseMatch):
                reverse("obtain_auth_token")

            with pytest.raises(Resolver404):
                resolve("/api/v1/auth-token/")

        self._reload_urls()
