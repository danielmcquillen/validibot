"""
Internal API router (APP_ROLE=worker only).

Contains endpoints that should not be exposed on the public web service, such as
validator callbacks and scheduled task endpoints.
"""

from django.urls import path

from validibot.core.api.scheduled_tasks import CleanupCallbackReceiptsView
from validibot.core.api.scheduled_tasks import CleanupIdempotencyKeysView
from validibot.core.api.scheduled_tasks import ClearSessionsView
from validibot.validations.api.callbacks import ValidationCallbackView

app_name = "api-internal"
urlpatterns = [
    # Validator callbacks (from Cloud Run Jobs)
    path(
        "validation-callbacks/",
        ValidationCallbackView.as_view(),
        name="validation-callbacks",
    ),
    # Scheduled tasks (from Cloud Scheduler)
    path(
        "scheduled/cleanup-idempotency-keys/",
        CleanupIdempotencyKeysView.as_view(),
        name="scheduled-cleanup-idempotency-keys",
    ),
    path(
        "scheduled/cleanup-callback-receipts/",
        CleanupCallbackReceiptsView.as_view(),
        name="scheduled-cleanup-callback-receipts",
    ),
    path(
        "scheduled/clear-sessions/",
        ClearSessionsView.as_view(),
        name="scheduled-clear-sessions",
    ),
]
