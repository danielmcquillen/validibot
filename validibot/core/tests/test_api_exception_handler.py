"""Tests for the API-wide JSON exception handler.

Why this test suite exists
--------------------------
Before ``validibot.core.api.exception_handler`` existed, any unhandled
Python exception raised in a DRF view would fall through to Django's
default 500 page (HTML). API clients can't parse HTML and would surface
useless error messages — or worse, retry-loop on a bug that should have
returned a structured error. The handler fixes that by intercepting
uncaught exceptions and returning a JSON 500 in the platform's standard
shape (``{"detail", "code"}``). The companion ``handler500`` view covers
exceptions that bypass DRF entirely (middleware-level crashes, URL
resolution failures).

These tests prove three load-bearing properties of that contract:

1.  **DRF-flavored exceptions still flow through DRF's default handler
    unchanged.** A custom EXCEPTION_HANDLER that broke ``ValidationError``
    rendering or threw away ``Retry-After`` headers on throttling would
    be worse than the bug it was meant to fix.

2.  **Uncaught exceptions return a JSON 500 with the platform's standard
    error shape**, with class/message detail only in DEBUG. Leaking
    exception class names or SQL fragments to API consumers is a real
    information-disclosure problem; this test pins production safety.

3.  **The Django ``handler500`` returns JSON for ``/api/*`` paths but
    HTML elsewhere.** A user hitting a broken HTML page should still see
    a friendly HTML error; a script hitting a broken API endpoint should
    get parseable JSON. The path-sniffing keeps both audiences happy.
"""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import patch

from rest_framework import status
from rest_framework.exceptions import NotFound
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIRequestFactory

from validibot.core.api.exception_handler import INTERNAL_ERROR_CODE
from validibot.core.api.exception_handler import api_exception_handler
from validibot.core.api.exception_handler import api_server_error_view

# ── DRF handler delegation ───────────────────────────────────────────
# These tests pin the contract that DRF-aware exceptions flow through
# DRF's default handler untouched. Breaking this would degrade every
# API error response in the platform, not just unhandled ones — so the
# coverage here is non-negotiable.


class TestDrfExceptionsPassThrough:
    """Behaviour: known DRF exceptions are formatted by DRF, not by us."""

    def test_validation_error_returns_400_via_drf(self):
        """A ValidationError must produce the standard DRF 400 response.

        DRF's default handler turns ``ValidationError`` into a structured
        400 with the field errors as the body. If our wrapper accidentally
        intercepted it as an "uncaught" exception, every form validation
        failure in the API would turn into an opaque 500.
        """
        exc = ValidationError({"email": ["This field is required."]})
        response = api_exception_handler(exc, context={})

        assert response is not None
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        # DRF preserves the field-level structure in the body.
        assert response.data == {"email": ["This field is required."]}

    def test_not_found_returns_404_via_drf(self):
        """An ``Http404``-equivalent DRF exception must produce a 404.

        Same reasoning as the validation case: not-found responses are
        a core part of the API contract and must not be mangled into 500s.
        """
        response = api_exception_handler(NotFound(), context={})

        assert response is not None
        assert response.status_code == status.HTTP_404_NOT_FOUND


# ── Uncaught exception fallback ──────────────────────────────────────
# The reason this module exists. Any non-DRF exception (AttributeError,
# KeyError, IntegrityError, etc.) must produce a JSON 500 in the
# platform's standard ``{"detail", "code"}`` shape — never HTML, never
# the raw exception class leaking to clients in production.


class TestUncaughtExceptionFallback:
    """Behaviour: arbitrary Python exceptions return a JSON 500."""

    def _make_context(self):
        """Build the DRF-style context dict the handler expects.

        DRF passes a context with ``request`` and ``view`` keys; the
        handler reads them for log enrichment. Tests don't care about
        the values, but ``logger.exception`` is called with them so
        they need to exist to avoid AttributeError in the handler.
        """
        factory = APIRequestFactory()
        return {
            "request": factory.post("/api/v1/some/endpoint/"),
            "view": None,
        }

    def test_attribute_error_returns_json_500(self, settings):
        """An AttributeError must become a 500 with code INTERNAL_SERVER_ERROR.

        This is the exact bug that motivated the handler:
        ``AgentBillingMode.AGENT_PAYS_ACP`` raised AttributeError, DRF
        returned None, Django rendered HTML. Pin the JSON behaviour so
        the regression can't recur silently.
        """
        settings.DEBUG = False
        exc = AttributeError(
            "type object 'AgentBillingMode' has no attribute 'AGENT_PAYS_ACP'",
        )

        response = api_exception_handler(exc, context=self._make_context())

        assert response is not None
        assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
        assert response.data == {
            "detail": "Internal server error.",
            "code": INTERNAL_ERROR_CODE,
        }

    def test_production_body_does_not_leak_exception_class(self, settings):
        """In production, the response body must not reveal the exception type.

        Leaking exception class names or messages to API clients is a
        small but real information-disclosure problem — it tells an
        attacker probing for weaknesses what kind of bug they triggered.
        Production responses must contain only the generic body.
        """
        settings.DEBUG = False
        exc = KeyError("secret_table_column_name")

        response = api_exception_handler(exc, context=self._make_context())

        assert response is not None
        body = response.data
        assert "debug" not in body
        # Exception class names like "KeyError" must not appear anywhere
        # in the response body. The serialised body is a dict; check
        # each value defensively.
        for value in body.values():
            assert "KeyError" not in str(value)
            assert "secret_table_column_name" not in str(value)

    def test_debug_body_includes_exception_class_and_message(self, settings):
        """In DEBUG, the response includes class+message under ``debug``.

        Local development is painful if every API error is an opaque
        500. The DEBUG branch surfaces just enough information — class
        name and message — to make iteration on integrations possible
        without leaking the full traceback (which still goes to logging).
        """
        settings.DEBUG = True
        exc = ValueError("decimal_latitude out of range")

        response = api_exception_handler(exc, context=self._make_context())

        assert response is not None
        body = response.data
        assert body["detail"] == "Internal server error."
        assert body["code"] == INTERNAL_ERROR_CODE
        assert body["debug"] == {
            "exception": "ValueError",
            "message": "decimal_latitude out of range",
        }

    def test_uncaught_exception_is_logged_with_full_traceback(self, settings):
        """The handler must call ``logger.exception`` for every fall-through.

        Without the log call, an uncaught exception would silently turn
        into a generic 500 — operators would have no traceback to
        diagnose the underlying bug. ``logger.exception`` is what
        attaches the traceback to the log record (and feeds Sentry, via
        the existing Django logging integration).
        """
        settings.DEBUG = False
        exc = RuntimeError("downstream service is on fire")

        with patch(
            "validibot.core.api.exception_handler.logger",
        ) as mock_logger:
            api_exception_handler(exc, context=self._make_context())

        assert mock_logger.exception.called, (
            "Uncaught exceptions must be logged via logger.exception so "
            "the traceback reaches operator logs and Sentry."
        )


# ── Django handler500 path detection ─────────────────────────────────
# Exceptions raised outside DRF's dispatch cycle (middleware, URL
# resolution, signal handlers) bypass the DRF handler entirely. The
# ``api_server_error_view`` registered as Django's ``handler500``
# catches those — but only converts to JSON for ``/api/*`` paths so
# the human-facing HTML 500 page remains for everything else.


class TestApiServerErrorView:
    """Behaviour: ``handler500`` returns JSON for API paths, HTML elsewhere."""

    def test_api_path_returns_json_500(self):
        """A 500 on ``/api/v1/anything/`` must return JSON.

        This is the safety net for crashes that happen before DRF's
        dispatch runs — for example, a middleware exception or a URL
        resolution failure inside an authenticated request. Without
        this, API clients would still see HTML for those edge cases
        even after the DRF EXCEPTION_HANDLER fix lands.
        """
        factory = APIRequestFactory()
        request = factory.get("/api/v1/orgs/foo/workflows/")

        response = api_server_error_view(request)

        assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
        assert response["Content-Type"].startswith("application/json")
        # The body is JSON-encoded; decode and assert the shape.
        import json

        body = json.loads(response.content)
        assert body == {
            "detail": "Internal server error.",
            "code": INTERNAL_ERROR_CODE,
        }

    def test_non_api_path_falls_through_to_default_500(self):
        """A 500 on ``/app/something/`` must use Django's default HTML 500.

        Human users hitting a broken UI route should see the friendly
        HTML 500 page (rendered by Django's ``server_error`` view), not
        a JSON blob. The path sniffing on ``/api/`` is the simplest
        way to discriminate the two audiences without per-view
        configuration.
        """
        factory = APIRequestFactory()
        request = factory.get("/app/validations/some-uuid/")

        with patch(
            "django.views.defaults.server_error",
        ) as mock_default:
            mock_default.return_value = "<html>500</html>"
            response = api_server_error_view(request)

        mock_default.assert_called_once_with(request)
        assert response == "<html>500</html>"
