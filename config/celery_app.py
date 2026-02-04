"""
Celery application configuration for Validibot.

This module configures Celery for background task processing in
Docker Compose deployments. Tasks are defined using @shared_task decorator
to ensure compatibility with CELERY_TASK_ALWAYS_EAGER in tests.

Components:
  - Worker: Processes background tasks (`celery -A config worker`)
  - Beat: Triggers periodic tasks (`celery -A config beat`)

For GCP deployments, tasks are dispatched via Google Cloud Tasks instead.
See validibot/core/tasks/dispatch/ for the dispatcher abstraction.

Configuration:
  - Broker: Redis (CELERY_BROKER_URL)
  - Result backend: None (fire-and-forget, all state in Django models)
  - Task serialization: JSON
  - Periodic tasks: django-celery-beat with DatabaseScheduler

Usage:
    # Run worker (single process, prefork pool)
    celery -A config worker --loglevel=info --concurrency=1

    # Run beat scheduler
    celery -A config beat --loglevel=info \\
        --scheduler django_celery_beat.schedulers:DatabaseScheduler

See docs/dev_docs/how-to/configure-scheduled-tasks.md for details.
"""

import logging
import os

from celery import Celery

logger = logging.getLogger(__name__)

# Set the default Django settings module for the 'celery' program.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.local")

app = Celery("validibot")

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Load task modules from all registered Django apps.
app.autodiscover_tasks()


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Debug task for testing Celery connectivity."""
    logger.info("Debug task received: %r", self.request)
