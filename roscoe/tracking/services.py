from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any
from xml.dom import NotFoundErr

from django.db import IntegrityError
from django.utils.translation import gettext_lazy as _

from roscoe.events.constants import AppEventType
from roscoe.tracking.constants import TrackingEventType
from roscoe.tracking.models import TrackingEvent

if TYPE_CHECKING:
    from collections.abc import Mapping

    from roscoe.projects.models import Project
    from roscoe.users.models import Organization
    from roscoe.users.models import User
    from roscoe.workflows.models import Workflow


logger = logging.getLogger(__name__)


class TrackingEventService:
    """Encapsulates helpers for recording tracking events."""

    def log_tracking_event(
        self,
        *,
        event_type: str,
        app_event_type: AppEventType | str | None = None,
        project: Project | None,
        org: Organization | None,
        user: User | None = None,
        extra_data: Mapping[str, Any] | None = None,
    ) -> TrackingEvent | None:
        """
        Persist a tracking event if the required context is available.

        Args:
            event_type: Canonical identifier for the event being logged.
            project: Project context for the event. Required; skip logging if missing.
            org: Organization associated with the event.
            user: Optional user who triggered the event.
            extra_data: Optional structured metadata to store alongside the event.

        Returns:
            TrackingEvent instance when recorded, otherwise ``None`` if skipped or failed.
        """
        tracking_event = None
        try:
            tracking_event = self._log_tracking_event(
                event_type=event_type,
                app_event_type=app_event_type,
                project=project,
                org=org,
                user=user,
                extra_data=extra_data,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Unexpected error while logging tracking event",
                extra={
                    "event_type": event_type,
                    "project_id": getattr(project, "pk", None),
                    "org_id": getattr(org, "pk", None),
                    "user_id": getattr(user, "pk", None),
                    "app_event_type": app_event_type,
                },
            )
        return tracking_event

    def _log_tracking_event(
        self,
        *,
        event_type: str,
        app_event_type: AppEventType | str | None = None,
        project: Project | None,
        org: Organization | None,
        user: User | None = None,
        extra_data: Mapping[str, Any] | None = None,
    ) -> TrackingEvent | None:
        if not user:
            raise ValueError(_("User is required to log a tracking event"))

        resolved_type = self._resolve_event_type(event_type)
        resolved_app_event_type = self._resolve_app_event_type(resolved_type, app_event_type)

        prepared_extra = self._prepare_extra_data(extra_data)
        actor = user if getattr(user, "is_authenticated", False) else None

        tracking_event = TrackingEvent.objects.create(
            project=project,
            org=org,
            user=actor,
            event_type=resolved_type,
            app_event_type=resolved_app_event_type,
            extra_data=prepared_extra,
        )
        return tracking_event

    def log_validation_run_started(
        self,
        *,
        workflow: Workflow,
        project: Project | None,
        user: User | None,
        submission_id: Any | None = None,
        validation_run_id: Any | None = None,
        extra_data: Mapping[str, Any] | None = None,
    ) -> TrackingEvent | None:
        """Public method for logging workflow start events."""

        tracking_event = None
        try:
            tracking_event = self._log_validation_run_started(
                workflow=workflow,
                project=project,
                user=user,
                submission_id=submission_id,
                validation_run_id=validation_run_id,
                extra_data=extra_data,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Unexpected error while logging validation run started event",
                extra={
                    "workflow_id": getattr(workflow, "pk", None),
                    "workflow_uuid": getattr(workflow, "uuid", None),
                    "workflow_slug": getattr(workflow, "slug", None),
                    "workflow_version": getattr(workflow, "version", None),
                    "project_id": getattr(project, "pk", None),
                    "user_id": getattr(user, "pk", None),
                    "submission_id": submission_id,
                    "validation_run_id": validation_run_id,
                },
            )
        return tracking_event

    def _log_validation_run_started(
        self,
        *,
        workflow: Workflow,
        project: Project | None,
        user: User | None,
        submission_id: Any | None = None,
        validation_run_id: Any | None = None,
        extra_data: Mapping[str, Any] | None = None,
    ) -> TrackingEvent | None:
        """Convenience helper for logging workflow start events."""

        event_project = project or getattr(workflow, "project", None)

        payload: dict[str, Any] = {
            "workflow_pk": getattr(workflow, "pk", None),
            "workflow_uuid": getattr(workflow, "uuid", None),
            "workflow_slug": getattr(workflow, "slug", None),
            "workflow_version": getattr(workflow, "version", None),
        }
        if submission_id is not None:
            payload["submission_id"] = submission_id
        if validation_run_id is not None:
            payload["validation_run_id"] = validation_run_id
        if extra_data:
            payload.update(extra_data)

        tracking_event = self.log_tracking_event(
            event_type=TrackingEventType.APP_EVENT,
            app_event_type=AppEventType.VALIDATION_RUN_STARTED,
            project=event_project,
            org=getattr(workflow, "org", None),
            user=user,
            extra_data=payload,
        )

        return tracking_event

    def _resolve_event_type(self, event_type: str) -> str:
        if event_type in TrackingEventType.values:
            return event_type
        logger.warning("Unknown tracking event type '%s'", event_type)
        return TrackingEventType.CUSTOM_EVENT

    def _resolve_app_event_type(
        self,
        event_type: str,
        app_event_type: AppEventType | str | None,
    ) -> str | None:
        if event_type != TrackingEventType.APP_EVENT:
            return None

        if app_event_type is None:
            logger.warning("APP_EVENT tracking requires an app_event_type value")
            return None

        value = (
            app_event_type.value
            if isinstance(app_event_type, AppEventType)
            else str(app_event_type)
        )
        if value not in AppEventType.values:
            logger.warning("Unknown application event '%s'", value)
            return value
        return value

    def _prepare_extra_data(
        self,
        extra_data: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not extra_data:
            return None
        processed: dict[str, Any] = {}
        for key, value in extra_data.items():
            processed[key] = self._normalize_extra_value(value)
        return processed

    def _normalize_extra_value(self, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (list, tuple)):
            return [self._normalize_extra_value(item) for item in value]
        if isinstance(value, dict):
            return {key: self._normalize_extra_value(val) for key, val in value.items()}
        return str(value)
