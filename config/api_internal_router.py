"""
Internal API router (APP_ROLE=worker only).

Contains endpoints that should not be exposed on the public web service, such as
validator callbacks, Cloud Tasks execution endpoints, and scheduled task endpoints.
"""

from django.urls import path

from validibot.core.api.scheduled_tasks import CleanupCallbackReceiptsView
from validibot.core.api.scheduled_tasks import CleanupIdempotencyKeysView
from validibot.core.api.scheduled_tasks import CleanupStuckRunsView
from validibot.core.api.scheduled_tasks import ClearSessionsView
from validibot.core.api.scheduled_tasks import ProcessPurgeRetriesView
from validibot.core.api.scheduled_tasks import PurgeExpiredSubmissionsView
from validibot.validations.api.callbacks import ValidationCallbackView
from validibot.validations.api.execute import ExecuteValidationRunView

app_name = "api-internal"
urlpatterns = [
    # Cloud Tasks execution (from Cloud Tasks queue)
    path(
        "execute-validation-run/",
        ExecuteValidationRunView.as_view(),
        name="execute-validation-run",
    ),
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
    path(
        "scheduled/purge-expired-submissions/",
        PurgeExpiredSubmissionsView.as_view(),
        name="scheduled-purge-expired-submissions",
    ),
    path(
        "scheduled/process-purge-retries/",
        ProcessPurgeRetriesView.as_view(),
        name="scheduled-process-purge-retries",
    ),
    path(
        "scheduled/cleanup-stuck-runs/",
        CleanupStuckRunsView.as_view(),
        name="scheduled-cleanup-stuck-runs",
    ),
]
