from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path(
        "support-message/",
        views.submit_support_message,
        name="support_message_create",
    ),
]
