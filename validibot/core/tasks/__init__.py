"""
Cloud Tasks integration for async task execution.

This module provides utilities for enqueueing tasks to Google Cloud Tasks,
which are then delivered to the worker service for processing.
"""

from validibot.core.tasks.cloud_tasks import enqueue_validation_run

__all__ = ["enqueue_validation_run"]
