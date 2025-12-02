from __future__ import annotations

import uuid
from datetime import timedelta
from typing import TYPE_CHECKING

from django.utils import timezone

from simplevalidations.events.constants import AppEventType
from simplevalidations.tracking.constants import TrackingEventType
from simplevalidations.tracking.models import TrackingEvent
from simplevalidations.tracking.services import TrackingEventService
from simplevalidations.validations.constants import ValidationRunStatus

if TYPE_CHECKING:  # pragma: no cover
    from simplevalidations.projects.models import Project
    from simplevalidations.users.models import Organization
    from simplevalidations.users.models import User
    from simplevalidations.workflows.models import Workflow


def _coerce_org(org, workflow, project):
    if org:
        return org
    if workflow and getattr(workflow, "org", None):
        return workflow.org
    if project and getattr(project, "org", None):
        return project.org
    return None


def seed_sample_tracking_data(
    *,
    org: Organization,
    project: Project | None = None,
    workflow: Workflow | None = None,
    user: User | None = None,
    days: int = 7,
    runs_per_day: int = 4,
    logins_per_day: int = 2,
    include_failures: bool = True,
    service: TrackingEventService | None = None,
) -> list[TrackingEvent]:
    """
    Generate synthetic tracking events for dashboards/tests.

    Args:
        org: Organization used for scoping.
        project: Optional project context; if omitted we do not attach project.
        workflow: Optional workflow metadata used in run events.
        user: Actor associated with events; defaults to first org user if available.
        days: Time span (backwards from now) to seed.
        runs_per_day: Number of validation run sequences per day.
        logins_per_day: Number of user login events per day.
        include_failures: Whether to sprinkle failed runs.
        service: Optional injected TrackingEventService (useful for tests).

    Returns:
        List of TrackingEvent objects that were created.
    """
    if days <= 0:
        return []
    service = service or TrackingEventService()
    org = _coerce_org(org, workflow, project)
    if not org:
        raise ValueError("An organization is required to seed tracking data.")
    if not user:
        user = org.users.order_by("id").first()
    now = timezone.now()
    events: list[TrackingEvent] = []

    seeded_flag = {"seeded": True}

    for day_offset in range(days):
        day_start = (now - timedelta(days=day_offset)).replace(
            hour=9,
            minute=0,
            second=0,
            microsecond=0,
        )

        for login_index in range(logins_per_day):
            login_time = day_start - timedelta(hours=login_index + 1)
            event = service.log_tracking_event(
                event_type=TrackingEventType.APP_EVENT,
                app_event_type=AppEventType.USER_LOGGED_IN,
                project=project,
                org=org,
                user=user,
                extra_data={**seeded_flag, "channel": "web"},
                channel="web",
                recorded_at=login_time,
            )
            if event:
                events.append(event)

        for run_index in range(runs_per_day):
            run_base_time = day_start + timedelta(hours=run_index * 2)
            submission_id = uuid.uuid4().hex
            run_identifier = uuid.uuid4().hex
            channel = "api" if (run_index + day_offset) % 2 == 0 else "web"
            created_event = service.log_validation_run_created(
                workflow=workflow,
                project=project,
                org=org,
                user=user,
                submission_id=submission_id,
                validation_run_id=run_identifier,
                extra_data={**seeded_flag, "channel": channel},
                channel=channel,
                recorded_at=run_base_time,
            )
            if created_event:
                events.append(created_event)

            started_event = service.log_validation_run_started(
                workflow=workflow,
                project=project,
                user=user,
                submission_id=submission_id,
                validation_run_id=run_identifier,
                extra_data={
                    **seeded_flag,
                    "status": ValidationRunStatus.RUNNING,
                    "channel": channel,
                },
                channel=channel,
                recorded_at=run_base_time + timedelta(minutes=5),
            )
            if started_event:
                events.append(started_event)

            is_failure = include_failures and (run_index + day_offset) % 5 == 0
            final_event_type = (
                AppEventType.VALIDATION_RUN_FAILED
                if is_failure
                else AppEventType.VALIDATION_RUN_SUCCEEDED
            )
            status_value = (
                ValidationRunStatus.FAILED
                if is_failure
                else ValidationRunStatus.SUCCEEDED
            )
            completion_event = service.log_validation_run_event(
                workflow=workflow,
                project=project,
                org=org,
                actor=user,
                event_type=final_event_type,
                submission_id=submission_id,
                validation_run_id=run_identifier,
                extra_data={
                    **seeded_flag,
                    "status": status_value,
                    "channel": channel,
                },
                channel=channel,
                recorded_at=run_base_time + timedelta(minutes=15),
            )
            if completion_event:
                events.append(completion_event)

    return events
