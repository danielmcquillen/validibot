from __future__ import annotations

from simplevalidations.notifications.models import Notification


def notifications_context(request):
    """
    Provide unread notification count for the current user and org.
    """

    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    unread_count = Notification.objects.filter(
        user=request.user,
        read_at__isnull=True,
        dismissed_at__isnull=True,
    ).count()
    return {
        "unread_notification_count": unread_count,
    }
