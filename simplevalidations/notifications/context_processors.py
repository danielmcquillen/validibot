from __future__ import annotations

from simplevalidations.notifications.models import Notification


def notifications_context(request):
    """
    Provide unread notification count for the current user and org.
    """

    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    org = getattr(request, "active_org", None) or getattr(request.user, "current_org", None)
    if not org:
        return {}
    unread_count = Notification.objects.filter(user=request.user, org=org, read_at__isnull=True).count()
    return {
        "unread_notification_count": unread_count,
    }
