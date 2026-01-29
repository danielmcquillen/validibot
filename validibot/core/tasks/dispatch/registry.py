"""
Task dispatcher factory.

Provides a factory function to get the appropriate task dispatcher
based on the DEPLOYMENT_TARGET setting.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

from validibot.core.constants import DeploymentTarget
from validibot.core.deployment import get_deployment_target

if TYPE_CHECKING:
    from validibot.core.tasks.dispatch.base import TaskDispatcher

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_task_dispatcher() -> TaskDispatcher:
    """
    Get the task dispatcher for the configured deployment target.

    Returns:
        TaskDispatcher instance for the current deployment target.

    Raises:
        ValueError: If DEPLOYMENT_TARGET is not set or no dispatcher exists.
    """
    # Import here to avoid circular imports
    from validibot.core.tasks.dispatch.dramatiq_dispatcher import DramatiqDispatcher
    from validibot.core.tasks.dispatch.google_cloud_tasks import (
        GoogleCloudTasksDispatcher,
    )
    from validibot.core.tasks.dispatch.local_dev import LocalDevDispatcher
    from validibot.core.tasks.dispatch.test_dispatcher import TestDispatcher

    target = get_deployment_target()

    dispatchers: dict[DeploymentTarget, type[TaskDispatcher]] = {
        DeploymentTarget.TEST: TestDispatcher,
        DeploymentTarget.LOCAL_DOCKER_COMPOSE: LocalDevDispatcher,
        DeploymentTarget.DOCKER_COMPOSE: DramatiqDispatcher,
        DeploymentTarget.GCP: GoogleCloudTasksDispatcher,
        # AWS not yet implemented
    }

    dispatcher_class = dispatchers.get(target)
    if not dispatcher_class:
        msg = f"No task dispatcher implemented for deployment target: {target.value}"
        raise ValueError(msg)

    dispatcher = dispatcher_class()

    logger.info(
        "Initialized task dispatcher: %s (sync=%s) for DEPLOYMENT_TARGET=%s",
        dispatcher.dispatcher_name,
        dispatcher.is_sync,
        target.value,
    )

    return dispatcher


def clear_dispatcher_cache() -> None:
    """Clear the cached dispatcher instance."""
    get_task_dispatcher.cache_clear()
