"""Tests for cross-deployment task delivery safety bounds.

Self-hosted Redis visibility must outlast a legitimate Celery task or it can
redeliver work while the first worker still runs. GCP HTTP delivery should be
short because it only orchestrates Cloud Run work, not performs validator
compute. These assertions keep those operational assumptions explicit.
"""

from django.conf import settings

MIN_CLOUD_TASKS_DEADLINE_SECONDS = 15
MAX_CLOUD_TASKS_DEADLINE_SECONDS = 30 * 60


def test_redis_visibility_exceeds_celery_hard_task_limit():
    """A healthy long task must not become visible to a second worker."""
    assert (
        settings.CELERY_BROKER_TRANSPORT_OPTIONS["visibility_timeout"]
        > settings.CELERY_TASK_TIME_LIMIT
    )


def test_cloud_tasks_deadline_is_bounded_for_short_orchestration():
    """Cloud Tasks should retry a wedged worker before its platform maximum."""
    assert (
        MIN_CLOUD_TASKS_DEADLINE_SECONDS
        <= settings.CLOUD_TASKS_DISPATCH_DEADLINE_SECONDS
        <= MAX_CLOUD_TASKS_DEADLINE_SECONDS
    )
