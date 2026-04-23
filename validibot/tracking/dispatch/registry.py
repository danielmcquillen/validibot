"""
Tracking dispatcher factory — pick and cache the dispatcher for the
current DEPLOYMENT_TARGET.

Mirrors :mod:`validibot.core.tasks.dispatch.registry`:

* ``lru_cache(maxsize=1)`` caches the dispatcher instance per process
  so dispatchers can do one-time setup in ``__init__`` (e.g., build a
  ``CloudTasksClient``) without re-running it on every signal.
* ``clear_tracking_dispatcher_cache()`` lets tests reset between
  cases when they want to exercise different targets.

Why a factory rather than a plain module-level singleton: tests
routinely switch ``DEPLOYMENT_TARGET`` via ``override_settings``, and
the factory re-reads the setting on every cache miss.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

from validibot.core.constants import DeploymentTarget
from validibot.core.deployment import get_deployment_target

if TYPE_CHECKING:
    from validibot.tracking.dispatch.base import TrackingDispatcher

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_tracking_dispatcher() -> TrackingDispatcher:
    """Return the tracking dispatcher for the current deployment target.

    Raises ``ValueError`` only for a target that has no dispatcher
    implementation — the expected stable set (test, local dev, docker
    compose, GCP) is all covered. AWS is explicitly not implemented
    yet; adding it is a matter of writing a new dispatcher and a
    mapping entry.
    """
    # Local imports avoid a module-level cycle: each dispatcher may
    # import shared helpers that indirectly re-enter this module
    # during its own import.
    from validibot.tracking.dispatch.celery_dispatcher import CeleryTrackingDispatcher
    from validibot.tracking.dispatch.cloud_tasks import CloudTasksTrackingDispatcher
    from validibot.tracking.dispatch.inline import InlineTrackingDispatcher

    target = get_deployment_target()

    # Inline handles both TEST and LOCAL_DEV. TEST is obvious; LOCAL_DEV
    # is the developer-laptop "no Celery worker, no Cloud Tasks" scenario
    # where writing synchronously is simpler than standing up a broker
    # just to see tracking events land.
    dispatchers: dict[DeploymentTarget, type[TrackingDispatcher]] = {
        DeploymentTarget.TEST: InlineTrackingDispatcher,
        DeploymentTarget.LOCAL_DOCKER_COMPOSE: CeleryTrackingDispatcher,
        DeploymentTarget.DOCKER_COMPOSE: CeleryTrackingDispatcher,
        DeploymentTarget.GCP: CloudTasksTrackingDispatcher,
    }

    dispatcher_class = dispatchers.get(target)
    if dispatcher_class is None:
        msg = (
            f"No tracking dispatcher implemented for deployment target: {target.value}"
        )
        raise ValueError(msg)

    dispatcher = dispatcher_class()
    logger.info(
        "Initialised tracking dispatcher: %s (sync=%s) for DEPLOYMENT_TARGET=%s",
        dispatcher.dispatcher_name,
        dispatcher.is_sync,
        target.value,
    )
    return dispatcher


def clear_tracking_dispatcher_cache() -> None:
    """Drop the cached dispatcher so the next call re-selects from
    the current ``DEPLOYMENT_TARGET``.

    Tests call this after ``override_settings``. Production code never
    needs it — the cache is process-local and workers are restarted,
    not dynamically re-targeted.
    """
    get_tracking_dispatcher.cache_clear()
