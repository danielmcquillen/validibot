from __future__ import annotations

from django.contrib.auth.signals import user_logged_in
from django.contrib.auth.signals import user_logged_out
from django.dispatch import receiver

from validibot.events.constants import AppEventType
from validibot.tracking.constants import TrackingEventType
from validibot.tracking.services import TrackingEventService


def _extract_org(user):
    if hasattr(user, "get_current_org"):
        try:
            return user.get_current_org()
        except Exception:  # pragma: no cover - defensive
            return None
    return None


def _build_request_metadata(request):
    if not request:
        return {}
    meta = {}
    user_agent = request.META.get("HTTP_USER_AGENT")
    if user_agent:
        meta["user_agent"] = user_agent
    path = getattr(request, "path", None)
    if path:
        meta["path"] = path
    return meta


@receiver(user_logged_in)
def log_user_logged_in(sender, request, user, **kwargs):
    service = TrackingEventService()
    org = _extract_org(user)
    metadata = _build_request_metadata(request)
    channel = "api" if metadata.get("path", "").startswith("/api") else "web"
    service.log_tracking_event(
        event_type=TrackingEventType.APP_EVENT,
        app_event_type=AppEventType.USER_LOGGED_IN,
        project=None,
        org=org,
        user=user,
        extra_data={k: v for k, v in metadata.items() if v},
        channel=channel,
    )


@receiver(user_logged_out)
def log_user_logged_out(sender, request, user, **kwargs):
    service = TrackingEventService()
    org = _extract_org(user)
    metadata = _build_request_metadata(request)
    channel = "api" if metadata.get("path", "").startswith("/api") else "web"
    service.log_tracking_event(
        event_type=TrackingEventType.APP_EVENT,
        app_event_type=AppEventType.USER_LOGGED_OUT,
        project=None,
        org=org,
        user=user,
        extra_data={k: v for k, v in metadata.items() if v},
        channel=channel,
    )
