"""
Internal API router (APP_ROLE=worker only).

Contains endpoints that should not be exposed on the public web service, such as
validator callbacks.
"""

from django.urls import path

from validibot.validations.api.callbacks import ValidationCallbackView

app_name = "api-internal"
urlpatterns = [
    path(
        "validation-callbacks/",
        ValidationCallbackView.as_view(),
        name="validation-callbacks",
    ),
]
