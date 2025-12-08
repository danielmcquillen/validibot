"""
URL configuration for validations API endpoints.
"""

from django.urls import path

from .callbacks import ValidationCallbackView

app_name = "validations"

urlpatterns = [
    path(
        "validation-callbacks/",
        ValidationCallbackView.as_view(),
        name="validation-callbacks",
    ),
]
