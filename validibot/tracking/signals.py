"""
Login / logout tracking signal receivers.

Extracts context (org, user-agent, path, derived channel) from the
request on the signal thread, then enqueues a Celery task to write
the ``TrackingEvent`` row asynchronously. See refactor-step item
``[review-#11]``.

Why asynchronous:
- The signal fires on the auth-path critical section. An inline
  tracking insert adds DB round-trip latency to every login /
  logout, on top of whatever slower storage (separate replica,
  WAL-backed disk) tracking eventually moves to.
- ``transaction.on_commit`` guarantees the enqueue only happens
  when the surrounding transaction (if any) commits — an auth
  flow that rolls back will not produce a ghost login event.
"""

from __future__ import annotations

from django.contrib.auth.signals import user_logged_in
from django.contrib.auth.signals import user_logged_out
from django.db import transaction
from django.dispatch import receiver

from validibot.events.constants import AppEventType
from validibot.tracking.constants import TrackingEventType


def _extract_org(user):
    if user is None:
        return None
    if hasattr(user, "get_current_org"):
        try:
            return user.get_current_org()
        except Exception:  # pragma: no cover - defensive
            return None
    return None


def _build_request_metadata(request) -> dict[str, str]:
    if not request:
        return {}
    meta: dict[str, str] = {}
    user_agent = request.headers.get("user-agent")
    if user_agent:
        meta["user_agent"] = user_agent
    path = getattr(request, "path", None)
    if path:
        meta["path"] = path
    return meta


def _derive_channel(metadata: dict[str, str]) -> str:
    return "api" if metadata.get("path", "").startswith("/api") else "web"


def _enqueue_tracking_event(
    *,
    app_event_type: AppEventType,
    user_id: int | None,
    org_id: int | None,
    extra_data: dict[str, str],
    channel: str,
) -> None:
    """Enqueue a tracking-event write via Celery on transaction commit.

    Local import of ``log_tracking_event_task`` keeps ``signals.py``
    importable without Celery being fully configured (e.g. during
    Django's ``makemigrations`` or check phases).
    """
    from validibot.tracking.tasks import log_tracking_event_task

    transaction.on_commit(
        lambda: log_tracking_event_task.delay(
            event_type=TrackingEventType.APP_EVENT,
            app_event_type=str(app_event_type),
            user_id=user_id,
            org_id=org_id,
            extra_data=extra_data or None,
            channel=channel,
        ),
    )


@receiver(user_logged_in)
def log_user_logged_in(sender, request, user, **kwargs):
    """Enqueue a USER_LOGGED_IN tracking event.

    All request-bound data (headers, path, current org) is captured
    here synchronously because ``request`` won't survive past the
    response boundary. Only primitives cross into the Celery task.
    """
    org = _extract_org(user)
    metadata = _build_request_metadata(request)
    channel = _derive_channel(metadata)
    extra_data = {k: v for k, v in metadata.items() if v}

    _enqueue_tracking_event(
        app_event_type=AppEventType.USER_LOGGED_IN,
        user_id=getattr(user, "pk", None),
        org_id=getattr(org, "pk", None) if org else None,
        extra_data=extra_data,
        channel=channel,
    )


@receiver(user_logged_out)
def log_user_logged_out(sender, request, user, **kwargs):
    """Enqueue a USER_LOGGED_OUT tracking event.

    ``user`` may be ``None`` when the logout request had no
    authenticated user attached (rare but valid). The task still
    records the event with ``user_id=None``; tracking analytics
    treats anonymous logouts as "session ended without attribution."
    """
    org = _extract_org(user)
    metadata = _build_request_metadata(request)
    channel = _derive_channel(metadata)
    extra_data = {k: v for k, v in metadata.items() if v}

    _enqueue_tracking_event(
        app_event_type=AppEventType.USER_LOGGED_OUT,
        user_id=getattr(user, "pk", None) if user else None,
        org_id=getattr(org, "pk", None) if org else None,
        extra_data=extra_data,
        channel=channel,
    )
