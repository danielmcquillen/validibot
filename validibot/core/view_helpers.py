"""
Shared helper utilities for Django/HTMX powered views.
"""

import json

from django.http import HttpResponse


def hx_trigger_response(
    message: str | None = None,
    level: str = "success",
    *,
    status_code: int = 204,
    close_modal: str | None = "workflowStepModal",
    extra_payload: dict[str, object] | None = None,
    include_steps_changed: bool = True,
) -> HttpResponse:
    """
    Build a generic HTMX response that triggers events/toasts on the client.
    """
    response = HttpResponse(status=status_code)
    payload: dict[str, object] = {}
    if include_steps_changed:
        payload["steps-changed"] = True
    if extra_payload:
        payload.update(extra_payload)
    if message:
        payload.setdefault("toast", {"level": level, "message": str(message)})
    if close_modal:
        payload["close-modal"] = close_modal
    if payload:
        response["HX-Trigger"] = json.dumps(payload)
    return response


def hx_redirect_response(url: str) -> HttpResponse:
    """
    Build an HTMX response instructing the browser to navigate to ``url``.
    """
    response = HttpResponse(status=204)
    response["HX-Redirect"] = url
    return response
