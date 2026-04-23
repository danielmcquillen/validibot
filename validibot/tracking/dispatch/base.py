"""
Abstract base class and request/response shape for tracking dispatchers.

This mirrors :mod:`validibot.core.tasks.dispatch.base` so the two
dispatcher hierarchies (validation-run, tracking-event) look and
behave identically from a caller's perspective. They deliberately do
not share a base class — their payloads are different, and forcing a
common ancestor would mean polluting both with fields the other
doesn't need.

Contract:

* ``dispatch()`` must not raise for transient or predictable failures
  (broker down, config missing, API error). Return a
  :class:`TrackingDispatchResponse` with ``error`` set instead.
  Callers on the auth-path critical section rely on this — a raised
  exception propagating into ``transaction.on_commit`` is exactly the
  failure mode the old direct-``.delay()`` code exhibited when Redis
  wasn't available on GCP.
* ``is_available()`` is a cheap, side-effect-free check that the
  dispatcher has what it needs to run. Used by health checks and
  tests; callers should not gate on it before dispatching, because
  dispatchers that *do* need to fail should return a descriptive
  error from ``dispatch()`` rather than silently skipping.
"""

from __future__ import annotations

import logging
from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TrackingEventRequest:
    """Primitive-only payload describing a single tracking event to record.

    Fields mirror the arguments accepted by
    :meth:`validibot.tracking.services.TrackingEventService.log_tracking_event`
    so the service layer is the single source of truth for what a
    tracking event actually *is*. The dispatcher's job is only to
    move this payload from the caller's process (usually the web
    request handling the auth signal) to wherever the service will
    be invoked (same process for inline / test, Celery worker for
    Docker Compose, Cloud Run worker for GCP).

    PKs-not-instances is deliberate: task queue serializers (Celery,
    Cloud Tasks JSON body) need primitives, and resolving a stale
    ORM instance from the signal-thread transaction could race with
    the worker-side read. Each backend resolves FKs freshly from the
    PKs when it's ready to write.
    """

    event_type: str
    """:class:`~validibot.tracking.constants.TrackingEventType` value."""

    app_event_type: str | None = None
    """:class:`~validibot.events.constants.AppEventType` value when
    ``event_type`` is ``APP_EVENT``. Passed as a string so the payload
    stays JSON-serialisable across Cloud Tasks hops."""

    user_id: int | None = None
    """PK of the acting :class:`~validibot.users.models.User`, if any."""

    org_id: int | None = None
    """PK of the :class:`~validibot.users.models.Organization` context."""

    project_id: int | None = None
    """PK of the :class:`~validibot.projects.models.Project` context."""

    extra_data: dict[str, Any] | None = None
    """Structured metadata (user-agent, request path, etc.)."""

    channel: str | None = None
    """``"web"`` / ``"api"`` or ``None`` to let the service derive it."""

    def to_payload(self) -> dict[str, Any]:
        """JSON-serialisable dict for Cloud Tasks / Celery transport."""
        return {
            "event_type": self.event_type,
            "app_event_type": self.app_event_type,
            "user_id": self.user_id,
            "org_id": self.org_id,
            "project_id": self.project_id,
            "extra_data": self.extra_data,
            "channel": self.channel,
        }


@dataclass
class TrackingDispatchResponse:
    """Outcome of a single tracking dispatch call.

    ``task_id`` semantics match the validation-run dispatcher:

    * Cloud Tasks: full task resource name.
    * Celery: task UUID.
    * Inline / test: ``None`` (no external task created; the write
      either happened or was captured in ``error``).
    """

    task_id: str | None
    """Identifier the dispatcher assigned to the async task, if any."""

    is_sync: bool
    """Whether ``dispatch()`` blocked until the event was recorded.

    Tests and ``local_dev`` return True; Celery and Cloud Tasks return
    False. Callers generally don't branch on this, but it's useful for
    telemetry and for tests that want to assert "this path ran
    synchronously so the DB row is already there."
    """

    error: str | None = None
    """Populated when dispatch failed. The caller decides whether to
    retry, log, or swallow — for tracking the convention is to log at
    WARNING and move on, since the alternative is breaking the request
    that triggered the event."""


class TrackingDispatcher(ABC):
    """Base class for deployment-target-specific tracking dispatchers.

    Subclasses implement exactly one thing: "take this request and
    get the matching service call to happen somewhere." Where
    "somewhere" is depends on the deployment target — inline in this
    process, a Celery worker across a Redis broker, a Cloud Run
    worker via Cloud Tasks.

    The class is deliberately thin: no shared state, no caching, no
    on-commit wrapping. Each concern lives where it belongs. The
    :func:`~validibot.tracking.dispatch.registry.get_tracking_dispatcher`
    factory caches the instance per process, so ``__init__`` can do
    work that should happen once (e.g., instantiate a Cloud Tasks
    client) — but most dispatchers don't need it.
    """

    @property
    @abstractmethod
    def dispatcher_name(self) -> str:
        """Human-readable identifier (e.g., ``"cloud_tasks"``).

        Used in log messages so operators can tell which backend
        handled (or failed to handle) a given event.
        """

    @property
    @abstractmethod
    def is_sync(self) -> bool:
        """Whether ``dispatch`` executes the write inline."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the dispatcher is configured and ready.

        Cheap, synchronous, no network calls. Intended for health
        checks and tests. Callers should not gate real dispatch on
        this — a misconfigured dispatcher should return an error from
        :meth:`dispatch`, not silently pass the call through.
        """

    @abstractmethod
    def dispatch(self, request: TrackingEventRequest) -> TrackingDispatchResponse:
        """Queue (or perform) the tracking event write.

        Must not raise. Return a response with ``error`` populated for
        any failure the dispatcher can reasonably detect. The raised-
        exception escape hatch is reserved for genuine programming
        errors (bad type, missing attribute) that should never happen
        at runtime — those will propagate and be caught by the
        signal receiver's on_commit safety net regardless.
        """
