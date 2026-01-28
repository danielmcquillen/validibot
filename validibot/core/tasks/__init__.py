"""
Task execution for Validibot.

This module provides two task execution mechanisms:

1. Cloud Tasks (GCP production):
   - Uses Google Cloud Tasks for async job queuing
   - Delivers tasks to worker service via HTTP

2. Dramatiq (self-hosted / local):
   - Uses Redis as message broker
   - Workers process tasks directly
   - Periodiq handles scheduled task triggering

For scheduled tasks, see scheduled_actors.py which defines periodic tasks
that run on cron schedules (session cleanup, expired data purge, etc.).
"""

from validibot.core.tasks.cloud_tasks import enqueue_validation_run

__all__ = ["enqueue_validation_run"]
