from django.urls import path

from validibot.notifications import views

app_name = "notifications"

urlpatterns = [
    path("", views.NotificationListView.as_view(), name="notification-list"),
    # Member invite (PendingInvite) acceptance
    path(
        "invite/<uuid:pk>/accept/",
        views.AcceptInviteView.as_view(),
        name="notification-invite-accept",
    ),
    path(
        "invite/<uuid:pk>/decline/",
        views.DeclineInviteView.as_view(),
        name="notification-invite-decline",
    ),
    # Guest invite (GuestInvite) acceptance
    path(
        "guest-invite/<uuid:pk>/accept/",
        views.AcceptGuestInviteView.as_view(),
        name="notification-guest-invite-accept",
    ),
    path(
        "guest-invite/<uuid:pk>/decline/",
        views.DeclineGuestInviteView.as_view(),
        name="notification-guest-invite-decline",
    ),
    # Dismiss any notification
    path(
        "dismiss/<uuid:pk>/",
        views.DismissNotificationView.as_view(),
        name="notification-dismiss",
    ),
]
