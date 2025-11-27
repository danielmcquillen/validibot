from django.urls import path

from simplevalidations.notifications import views

app_name = "notifications"

urlpatterns = [
    path("", views.NotificationListView.as_view(), name="notification-list"),
    path("invite/<uuid:pk>/accept/", views.AcceptInviteView.as_view(), name="notification-invite-accept"),
    path("invite/<uuid:pk>/decline/", views.DeclineInviteView.as_view(), name="notification-invite-decline"),
]
