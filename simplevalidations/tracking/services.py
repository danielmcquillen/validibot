from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any
from datetime import datetime

from django.utils.translation import gettext_lazy as _

from simplevalidations.events.constants import AppEventType
from simplevalidations.tracking.constants import TrackingEventType
from simplevalidations.tracking.models import TrackingEvent
from simplevalidations.validations.constants import ValidationRunStatus

if TYPE_CHECKING:
    from collections.abc import Mapping

    from simplevalidations.projects.models import Project
    from simplevalidations.users.models import Organization
    from simplevalidations.users.models import User
    from simplevalidations.workflows.models import Workflow
    from simplevalidations.validations.models import ValidationRun


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
        recorded_at: datetime | None = None,
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
            TrackingEvent instance when recorded, otherwise ``None`` if skipped
            or failed.
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
                recorded_at=recorded_at,
            )
        except Exception:
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
        recorded_at: datetime | None = None,
    ) -> TrackingEvent | None:
        if not event_type:
            raise ValueError(_("Event type is required to log a tracking event"))

        resolved_type = self._resolve_event_type(event_type)
        resolved_app_event_type = self._resolve_app_event_type(
            resolved_type,
            app_event_type,
        )

        prepared_extra = self._prepare_extra_data(extra_data)
        actor = user if getattr(user, "is_authenticated", False) else None

        if user is None:
            logger.debug(
                "Tracking event recorded without a user",
                extra={
                    "event_type": event_type,
                    "org_id": getattr(org, "pk", None),
                    "project_id": getattr(project, "pk", None),
                },
            )

        create_kwargs: dict[str, Any] = {
            "project": project,
            "org": org,
            "user": actor,
            "event_type": resolved_type,
            "app_event_type": resolved_app_event_type,
            "extra_data": prepared_extra,
        }
        if recorded_at:
            create_kwargs["created"] = recorded_at
            create_kwargs["modified"] = recorded_at

        tracking_event = TrackingEvent.objects.create(**create_kwargs)
        return tracking_event

    _RUN_STATUS_EVENT_MAP: dict[str, AppEventType] = {
        ValidationRunStatus.SUCCEEDED: AppEventType.VALIDATION_RUN_SUCCEEDED,
        ValidationRunStatus.FAILED: AppEventType.VALIDATION_RUN_FAILED,
        ValidationRunStatus.CANCELED: AppEventType.VALIDATION_RUN_CANCELED,
        ValidationRunStatus.TIMED_OUT: AppEventType.VALIDATION_RUN_TIMED_OUT,
    }

    def log_validation_run_event(
        self,
        *,
        run: "ValidationRun" | None = None,
        workflow: Workflow | None = None,
        project: Project | None = None,
        org: Organization | None = None,
        actor: User | None = None,
        event_type: AppEventType,
        submission_id: Any | None = None,
        validation_run_id: Any | None = None,
        extra_data: Mapping[str, Any] | None = None,
        recorded_at: datetime | None = None,
    ) -> TrackingEvent | None:
        """
        Generic helper for logging validation run lifecycle events.
        """

        if run is not None:
            workflow = workflow or getattr(run, "workflow", None)
            project = project or getattr(run, "project", None)
            if project is None and getattr(run, "submission", None):
                project = getattr(run.submission, "project", None)
            org = org or getattr(run, "org", None)
            if actor is None:
                actor = getattr(run, "user", None)
            if submission_id is None and getattr(run, "submission", None):
                submission_id = getattr(run.submission, "pk", None)
            if validation_run_id is None:
                validation_run_id = getattr(run, "pk", None)

        event_workflow = workflow or getattr(run, "workflow", None) if run else None
        event_project = project
        if event_project is None and run is not None:
            event_project = getattr(run, "project", None)
            if event_project is None and getattr(run, "submission", None):
                event_project = getattr(run.submission, "project", None)
        if event_project is None and event_workflow is not None:
            event_project = getattr(event_workflow, "project", None)
        event_org = org or getattr(event_workflow, "org", None)

        payload: dict[str, Any] = {}
        if event_workflow:
            payload.update(
                {
                    "workflow_pk": getattr(event_workflow, "pk", None),
                    "workflow_uuid": getattr(event_workflow, "uuid", None),
                    "workflow_slug": getattr(event_workflow, "slug", None),
                    "workflow_version": getattr(event_workflow, "version", None),
                },
            )
        if submission_id is not None:
            payload["submission_id"] = submission_id
        if validation_run_id is not None:
            payload["validation_run_id"] = validation_run_id
        if extra_data:
            payload.update(extra_data)

        cleaned_payload = {k: v for k, v in payload.items() if v is not None}

        return self.log_tracking_event(
            event_type=TrackingEventType.APP_EVENT,
            app_event_type=event_type,
            project=event_project,
            org=event_org,
            user=actor,
            extra_data=cleaned_payload or None,
            recorded_at=recorded_at,
        )

    def log_validation_run_created(
        self,
        *,
        run: "ValidationRun" | None = None,
        workflow: Workflow | None = None,
        project: Project | None = None,
        org: Organization | None = None,
        user: User | None = None,
        submission_id: Any | None = None,
        validation_run_id: Any | None = None,
        extra_data: Mapping[str, Any] | None = None,
        recorded_at: datetime | None = None,
    ) -> TrackingEvent | None:
        return self.log_validation_run_event(
            run=run,
            workflow=workflow,
            project=project,
            org=org,
            actor=user,
            event_type=AppEventType.VALIDATION_RUN_CREATED,
            submission_id=submission_id,
            validation_run_id=validation_run_id,
            extra_data=extra_data,
            recorded_at=recorded_at,
        )

    def log_validation_run_started(
        self,
        *,
        run: "ValidationRun" | None = None,
        workflow: Workflow | None = None,
        project: Project | None = None,
        user: User | None = None,
        submission_id: Any | None = None,
        validation_run_id: Any | None = None,
        extra_data: Mapping[str, Any] | None = None,
        recorded_at: datetime | None = None,
    ) -> TrackingEvent | None:
        """Public method for logging workflow start events."""

        tracking_event = None
        try:
            tracking_event = self.log_validation_run_event(
                run=run,
                workflow=workflow,
                project=project,
                org=getattr(workflow, "org", None) if workflow else None,
                actor=user,
                submission_id=submission_id,
                validation_run_id=validation_run_id,
                extra_data=extra_data,
                recorded_at=recorded_at,
                event_type=AppEventType.VALIDATION_RUN_STARTED,
            )
        except Exception:
            logger.exception(
                "Unexpected error while logging validation run started event",
                extra={
                    "workflow_id": getattr(workflow, "pk", None)
                    if workflow
                    else getattr(run, "workflow_id", None),
                    "workflow_uuid": getattr(workflow, "uuid", None)
                    if workflow
                    else getattr(getattr(run, "workflow", None), "uuid", None),
                    "workflow_slug": getattr(workflow, "slug", None)
                    if workflow
                    else getattr(getattr(run, "workflow", None), "slug", None),
                    "workflow_version": getattr(workflow, "version", None)
                    if workflow
                    else getattr(getattr(run, "workflow", None), "version", None),
                    "project_id": getattr(project, "pk", None),
                    "user_id": getattr(user, "pk", None),
                    "submission_id": submission_id,
                    "validation_run_id": validation_run_id,
                },
            )
        return tracking_event

    def log_validation_run_status(
        self,
        *,
        run: "ValidationRun",
        status: str,
        actor: User | None = None,
        extra_data: Mapping[str, Any] | None = None,
        recorded_at: datetime | None = None,
    ) -> TrackingEvent | None:
        status_value = (
            status.value
            if hasattr(status, "value")
            else str(status) if status is not None else None
        )
        if not status_value:
            return None
        event_type = self._RUN_STATUS_EVENT_MAP.get(status_value)
        if not event_type:
            logger.debug("No tracking event configured for run status '%s'", status_value)
            return None

        payload: dict[str, Any] = {"status": status_value}
        if extra_data:
            payload.update(extra_data)

        return self.log_validation_run_event(
            run=run,
            actor=actor,
            event_type=event_type,
            extra_data=payload,
            recorded_at=recorded_at,
        )

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
