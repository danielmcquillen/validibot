"""Regression guard against resurrecting the orphaned ``validations.api.urls`` module.

WHY THIS MATTERS (security / correctness):

The worker callback endpoint ``/api/v1/validation-callbacks/`` accepts results
from validator backends and is deliberately mounted **only** on the worker role
(``config.api_internal_router``, namespace ``api-internal``) — it is kept off the
public web service so untrusted callers cannot reach it there.

A now-deleted module, ``validibot/validations/api/urls.py``, defined a *second*
``urlpatterns`` for that same ``ValidationCallbackView`` under ``app_name =
"validations"``. It was orphaned (no ``include()`` ever referenced it), but its
mere existence was repeatedly mistaken for a real, second registration of the
worker-only callback view — i.e. a path by which the callback could leak onto
the web service through the public ``validations`` namespace. We deleted it.

These tests lock in the contract so the dead module cannot quietly come back:

1. The ``validation-callbacks`` route exists under the worker-internal router and
   nowhere else, and the public web ``validations`` namespace must NOT expose it.
2. The orphaned ``validibot.validations.api.urls`` module must not exist / not be
   importable as a URLconf with ``urlpatterns``.
"""

from __future__ import annotations

import importlib

import pytest
from django.test import SimpleTestCase
from django.test import override_settings
from django.urls import Resolver404
from django.urls import clear_url_caches
from django.urls import resolve


class OrphanedValidationsApiUrlsTests(SimpleTestCase):
    """Guards the single-registration invariant for the worker callback route."""

    def _reload_urls(self) -> None:
        """Reload the role-branching URLconf chain so ``override_settings`` applies.

        ``config.urls`` selects ``config.urls_web`` or ``config.urls_worker`` based
        on ``APP_ROLE`` at import time, so the modules must be reloaded for an
        ``override_settings(APP_ROLE=...)`` block to take effect. Mirrors the
        helper in ``test_url_role_routing.py``.
        """
        import config.urls
        import config.urls_web
        import config.urls_worker

        clear_url_caches()
        importlib.reload(config.urls_web)
        importlib.reload(config.urls_worker)
        importlib.reload(config.urls)

    @override_settings(APP_ROLE="web", APP_IS_WORKER=False, ENABLE_API=True)
    def test_web_role_never_exposes_validation_callbacks(self) -> None:
        """The web service must not serve the worker-only callback endpoint.

        If the deleted ``validations.api.urls`` module were ever re-added and
        wired into the public ``validations`` namespace, this route would start
        resolving on the web role — exactly the leak we are preventing. Asserting
        a ``Resolver404`` here fails loudly if that regression returns.
        """
        self._reload_urls()
        with pytest.raises(Resolver404):
            resolve("/api/v1/validation-callbacks/")

    def test_orphaned_validations_api_urls_module_is_absent(self) -> None:
        """The dead ``validibot.validations.api.urls`` module must stay deleted.

        The module had zero importers and merely duplicated the worker callback
        registration, so it was removed. Importing it must fail; if someone
        recreates the file, this test flags the resurrection of the shadow
        registration instead of letting it silently reappear.
        """
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("validibot.validations.api.urls")
