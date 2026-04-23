"""Tests for ``AuditContextMiddleware`` and the ``contextvars`` plumbing.

Three areas of coverage:

1. The middleware installs an ``AuditRequestContext`` that signal
   handlers can read via ``get_current_context()``.
2. The context is reset on the way out of the view — even if the view
   raises — so a later request on the same async task doesn't inherit
   a stale actor.
3. Client IP extraction prefers ``X-Forwarded-For`` (Cloud Run) and
   falls back to ``REMOTE_ADDR`` (bare-metal deployments).

We use ``RequestFactory`` rather than the Django test client because
the test client short-circuits the middleware stack in some scenarios;
``RequestFactory`` keeps the contract test honest.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory
from django.test import TestCase

from validibot.audit.context import AuditRequestContext
from validibot.audit.context import get_current_context
from validibot.audit.middleware import AuditContextMiddleware
from validibot.users.tests.factories import UserFactory


class AuditContextMiddlewareTests(TestCase):
    """Middleware installs + clears the per-request audit context."""

    def setUp(self) -> None:
        """Each test builds its own request — no shared state."""

        self.factory = RequestFactory()

    def test_authenticated_request_populates_actor(self) -> None:
        """A logged-in user on the request translates into an actor
        with that user attached, including IP and user-agent from the
        headers.
        """

        user = UserFactory()
        captured: dict[str, AuditRequestContext] = {}

        def fake_view(request):
            captured["context"] = get_current_context()
            return HttpResponse()

        middleware = AuditContextMiddleware(fake_view)
        request = self.factory.get(
            "/some-path/",
            HTTP_X_FORWARDED_FOR="203.0.113.5, 10.0.0.1",
            HTTP_USER_AGENT="TestClient/1.0",
        )
        request.user = user

        middleware(request)

        context = captured["context"]
        self.assertEqual(context.actor.user, user)
        self.assertEqual(context.actor.ip_address, "203.0.113.5")
        self.assertEqual(context.actor.user_agent, "TestClient/1.0")
        # request_id is "req_<hex>" — just check the prefix.
        self.assertTrue(context.request_id.startswith("req_"))

    def test_anonymous_user_produces_unattributed_actor(self) -> None:
        """Anonymous users should yield an actor with ``user=None``.

        The middleware must distinguish ``AnonymousUser`` from a real
        user — otherwise a login-failed audit entry would link to a
        useless pseudo-user row.
        """

        captured: dict[str, AuditRequestContext] = {}

        def fake_view(request):
            captured["context"] = get_current_context()
            return HttpResponse()

        middleware = AuditContextMiddleware(fake_view)
        request = self.factory.get("/")
        request.user = AnonymousUser()

        middleware(request)

        self.assertIsNone(captured["context"].actor.user)

    def test_context_is_reset_after_response(self) -> None:
        """After the middleware returns, subsequent reads of
        ``get_current_context()`` should see the empty fallback.

        Without this reset an audit write triggered later on the same
        async task would reuse the previous request's actor.
        """

        def fake_view(request):
            return HttpResponse()

        middleware = AuditContextMiddleware(fake_view)
        request = self.factory.get("/")
        request.user = AnonymousUser()

        middleware(request)

        after = get_current_context()
        self.assertIsNone(after.actor.user)
        self.assertEqual(after.request_id, "")

    def test_context_is_reset_even_when_view_raises(self) -> None:
        """An exception inside the view MUST NOT leak context to the
        next request. The middleware uses ``try/finally`` specifically
        so this invariant holds.
        """

        def raising_view(request):
            raise RuntimeError("boom")

        middleware = AuditContextMiddleware(raising_view)
        request = self.factory.get("/")
        request.user = AnonymousUser()

        with pytest.raises(RuntimeError):
            middleware(request)

        self.assertEqual(get_current_context().request_id, "")

    def test_ip_falls_back_to_remote_addr(self) -> None:
        """When no proxy header is present, REMOTE_ADDR is used.

        That's the correct path for deployments that expose Django
        directly (local dev, tests, bare-metal boxes not behind
        nginx/Cloud Run).
        """

        captured: dict[str, AuditRequestContext] = {}

        def fake_view(request):
            captured["context"] = get_current_context()
            return HttpResponse()

        middleware = AuditContextMiddleware(fake_view)
        request = self.factory.get("/")
        request.user = AnonymousUser()
        request.META["REMOTE_ADDR"] = "198.51.100.7"
        # No HTTP_X_FORWARDED_FOR.

        middleware(request)

        self.assertEqual(captured["context"].actor.ip_address, "198.51.100.7")
