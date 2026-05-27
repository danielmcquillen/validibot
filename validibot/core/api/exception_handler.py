"""Custom DRF exception handler that returns JSON for every API error.

Why this module exists
----------------------
The default DRF exception handler (``rest_framework.views.exception_handler``)
only catches DRF-flavored exceptions: ``APIException`` subclasses,
``Http404``, and Django ``PermissionDenied``. Any other Python exception that
escapes a DRF view — ``AttributeError``, ``KeyError``, ``IntegrityError``,
``TypeError``, anything raised in a non-DRF utility function — falls through
to Django's middleware, which renders the configured 500 template. That
template is HTML. API clients receive an HTML body, fail to parse it as
JSON, and surface useless error messages to their users.

This handler wraps DRF's default with a fallback that catches any unhandled
exception, logs it, and returns a JSON 500 response in the platform's
standard error shape (``{"detail", "code"}``). Together with the Django
``handler500`` registered in ``config/urls.py``, every error on an
``/api/`` path now returns JSON — regardless of where in the stack the
exception originated.

How it fits in
--------------
- Registered via ``REST_FRAMEWORK["EXCEPTION_HANDLER"]`` in
  ``config/settings/base.py``.
- DRF calls this for every exception raised in a DRF view. The default
  handler runs first; if it returned ``None`` (uncaught exception), we
  build the JSON 500.
- Out-of-view exceptions (middleware, URL resolution, signal handlers)
  bypass DRF entirely. Those are handled by the API-aware
  ``server_error`` view registered as ``handler500``.

Production safety
-----------------
In production (``DEBUG=False``), the response body is deliberately
generic — ``"Internal server error"`` plus a stable error code. The
full traceback goes to logging (and Sentry, via the existing Django
logging integration) but never to the API client. In ``DEBUG=True``,
the exception class and message are included in the response to help
local development. Tracebacks never appear in API responses.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from django.conf import settings
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_default_exception_handler

logger = logging.getLogger(__name__)

# Stable machine-readable error code returned in every JSON 500.
# Clients can branch on this instead of parsing the human-readable
# ``detail`` field, which may be translated.
INTERNAL_ERROR_CODE = "INTERNAL_SERVER_ERROR"


def api_exception_handler(exc: Exception, context: dict[str, Any]) -> Response:
    """Return a JSON response for every exception raised in a DRF view.

    DRF's built-in handler covers the well-known API exceptions
    (validation, permission, throttling, 404, 405). For any other
    exception it returns ``None``, which causes the framework to
    re-raise and the response to fall through to Django's default
    500 page (HTML). This wrapper intercepts that fall-through and
    builds a structured JSON 500 instead.

    Args:
        exc: The exception raised in the view.
        context: DRF context dict with ``view``, ``args``, ``kwargs``,
            and ``request``.

    Returns:
        A DRF ``Response`` with a JSON body. For DRF-flavored
        exceptions this is the default handler's response, unchanged.
        For uncaught exceptions it is a 500 with the platform's
        standard ``{"detail", "code"}`` error shape.
    """
    # Delegate to DRF's built-in handler first. It knows how to format
    # all the framework-aware exceptions (validation errors, throttling,
    # permission denied, etc.) consistently — we want those untouched.
    response = drf_default_exception_handler(exc, context)
    if response is not None:
        return response

    # DRF returned None, meaning this is an exception type the
    # framework doesn't know about — almost always a programming error
    # (AttributeError, KeyError) or an unwrapped infrastructure error
    # (IntegrityError, OperationalError). Log it with full traceback so
    # the operator/Sentry can diagnose, then return a generic JSON 500.
    request = context.get("request")
    view = context.get("view")
    logger.exception(
        "Unhandled exception in API view %s for %s %s",
        getattr(view, "__class__", type(view)).__name__ if view else "<unknown>",
        getattr(request, "method", "?"),
        getattr(request, "path", "?"),
    )

    return Response(
        _build_internal_error_body(exc),
        status=HTTPStatus.INTERNAL_SERVER_ERROR,
    )


def _build_internal_error_body(exc: Exception) -> dict[str, Any]:
    """Construct the JSON body for an uncaught-exception 500 response.

    Production responses (``DEBUG=False``) contain only the generic
    ``detail`` string and the stable ``code``. The exception class
    and message are deliberately omitted so we don't leak internals
    (class names, table names, SQL, file paths) to API consumers.

    Development responses (``DEBUG=True``) include the exception class
    and message under a ``debug`` key, to make local iteration on
    integrations less painful. Tracebacks never appear here — they
    belong in the developer's terminal / logging, not in an HTTP body.

    The shape matches the platform's documented API error contract
    (``detail``, ``code``, optional ``debug``) so clients can use one
    parser for every kind of API failure.

    Args:
        exc: The exception that fell through DRF's default handler.

    Returns:
        A serialisable dict ready to be returned as the response body.
    """
    body: dict[str, Any] = {
        "detail": "Internal server error.",
        "code": INTERNAL_ERROR_CODE,
    }
    if getattr(settings, "DEBUG", False):
        body["debug"] = {
            "exception": type(exc).__name__,
            "message": str(exc),
        }
    return body


def api_server_error_view(request, *args, **kwargs):
    """Django ``handler500`` view that returns JSON for API paths.

    DRF's ``EXCEPTION_HANDLER`` only fires for exceptions raised inside
    a DRF view's dispatch cycle. Exceptions raised earlier — in
    middleware, URL resolution, or signal handlers — bypass DRF and
    are handled by Django's ``handler500``. The default ``handler500``
    renders ``500.html``, which is HTML, which defeats the purpose of
    the DRF handler for API clients whose only fault was hitting a
    request that happens to crash in a non-view layer.

    This view sniffs the request path. For anything under ``/api/`` it
    returns a JSON 500 in the same shape as the DRF handler. For
    everything else it falls through to Django's default
    ``server_error`` view so the human-facing 500 page is unchanged.

    Wired up as ``handler500`` in ``config/urls.py``.

    Args:
        request: The Django request.

    Returns:
        Either a ``JsonResponse`` (for API paths) or whatever
        Django's default 500 view returns (for everything else).
    """
    # Import lazily so this module is import-safe when Django isn't
    # fully configured yet (e.g., during settings loading or in
    # specific test harnesses).
    from django.http import JsonResponse
    from django.views.defaults import server_error

    path = getattr(request, "path", "") or ""
    if path.startswith("/api/"):
        # We don't have the exception object here — Django doesn't pass
        # it to handler500 the way DRF does. The exception is already
        # logged by Django's error-reporting machinery, so we just
        # return the generic shape.
        body: dict[str, Any] = {
            "detail": "Internal server error.",
            "code": INTERNAL_ERROR_CODE,
        }
        return JsonResponse(body, status=HTTPStatus.INTERNAL_SERVER_ERROR)
    return server_error(request, *args, **kwargs)
