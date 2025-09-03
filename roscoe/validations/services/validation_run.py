from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from celery.exceptions import TimeoutError as CeleryTimeout
from django.conf import settings
from django.http import HttpRequest as Request
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from roscoe.validations.constants import ValidationRunStatus
from roscoe.validations.models import ValidationRun

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from roscoe.submissions.models import Submission
    from roscoe.users.models import Organization
    from roscoe.workflows.models import Workflow
    from roscoe.workflows.models import WorkflowStep


@dataclass
class ValidationRunTaskPayload:
    document: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    user_id: int | None = None


@dataclass
class ValidationRunTaskResult:
    run_id: int
    status: ValidationRunStatus
    result: dict | None = None
    error: str | None = None


class ValidationRunService:
    """
    Single service for 'launching' and then 'executing' validation runs.
    There are two main methods in this class:

    1. launch():    called by views to create a run and enqueue the Celery task.
    2. execute():   called by the Celery task to actually run the validation steps.

    """

    # ---------- Launch (views call this) ----------

    def launch(
        self,
        request: Request,
        org: Organization,
        workflow: Workflow,
        submission: Submission,
        metadata: dict | None = None,
    ) -> Response:
        """
        Creates a validation run for a given workflow and a user request.
        The user should have provided us with a 'submission' as part of their request.
        The submission is the document (json, xml, whatever) that the workflow will
        validate.

        We want to try to finish the validation run synchronously if possible,
        so we optimistically wait for a short period of time for the Celery task
        to complete. If it does, we return a 201 Created response with the run
        details. If it doesn't, we return a 202 Accepted response with a link
        to check the status of the run later.

        Args:
            request:        The HTTP request object.
            org:            The organization under which the validation run is created.
            workflow:       The workflow to be executed.
            submission:     The submission associated with the validation run.
            metadata:       Optional metadata to be associated with the run.

        Returns:
            Response: DRF Response object with appropriate status and data.

        """
        # local import to avoid cycles
        from roscoe.validations.tasks import execute_validation_run  # noqa:PLC0415

        if not request:
            err_msg = "Request object is required to build absolute URIs."
            raise ValueError(err_msg)
        if not org:
            err_msg = "Organization must be provided"
            raise ValueError(err_msg)
        if not request.user:
            err_msg = "Request user must be authenticated"
            raise ValueError(err_msg)
        if not submission:
            err_msg = "Submission must be provided"
            raise ValueError(err_msg)
        if not workflow.can_execute(user=request.user):
            err_msg = "User does not have permission to execute this workflow"
            raise PermissionError(err_msg)

        validation_run = ValidationRun.objects.create(
            org=org,
            workflow=workflow,
            submission=submission,
            project=None,  # TODO: set project if applicable
            status=ValidationRunStatus.PENDING,
        )
        try:
            if hasattr(submission, "latest_run_id"):
                submission.latest_run = validation_run
                submission.save(update_fields=["latest_run"])
        except Exception:
            logger.exception(
                "Failed to update submission.latest_run for submission",
                extra={"submission_id": submission.id},
            )

        async_result = execute_validation_run.apply_async(
            kwargs={
                "validation_run_id": validation_run.id,
                "metadata": metadata or {},
                "user_id": request.user.id,
            },
            countdown=2,  # slight delay to ensure DB transaction commits first
        )

        per_attempt = int(getattr(settings, "VALIDATION_START_ATTEMPT_TIMEOUT", 5))
        attempts = int(getattr(settings, "VALIDATION_START_ATTEMPTS", 4))

        terminal_statuses = [
            ValidationRunStatus.SUCCEEDED,
            ValidationRunStatus.FAILED,
            ValidationRunStatus.CANCELED,
            ValidationRunStatus.TIMED_OUT,
        ]

        for _ in range(attempts):
            with contextlib.suppress(CeleryTimeout):
                async_result.get(timeout=per_attempt, propagate=False)
            validation_run.refresh_from_db()
            if validation_run.status in terminal_statuses:
                break

        location = request.build_absolute_uri(
            reverse(
                "api:validationrun-detail",
                kwargs={"pk": validation_run.id},
            ),
        )

        if validation_run.status in terminal_statuses:
            from roscoe.validations.serializers import (  # noqa:PLC0415
                ValidationRunSerializer,
            )

            data = ValidationRunSerializer(validation_run).data
            response = Response(
                data,
                status=status.HTTP_201_CREATED,
                headers={"Location": location},
            )
            return response

        body = {
            "id": validation_run.id,
            "status": validation_run.status,
            "task_id": async_result.id,
            "detail": "Processing",
            "url": location,
            "poll": location,
        }
        headers = {"Location": location, "Retry-After": str(per_attempt)}
        response = Response(body, status=status.HTTP_202_ACCEPTED, headers=headers)

        return response

    # ---------- Execute (Celery tasks call this) ----------

    def execute(
        self,
        validation_run_id: int,
        user_id: int,
        metadata: dict | None = None,
    ) -> ValidationRunTaskResult:
        """
        This method executes a validation run given its ID and optional payload.
        It is meant to be called within a Celery task.

        Args:
            validation_run_id (int): The ID of the ValidationRun to execute.
            user_id (int): The ID of the user initiating the run.
            metadata (dict, optional): Additional metadata for the run.

        Returns:
            ValidationRunTaskResult: The result of the validation run execution.
        """
        validation_run: ValidationRun = ValidationRun.objects.select_related(
            "workflow",
            "org",
            "project",
            "submission",
        ).get(id=validation_run_id)

        if validation_run.status not in (
            ValidationRunStatus.PENDING,
            ValidationRunStatus.RUNNING,
        ):
            result = ValidationRunTaskResult(
                run_id=validation_run.id,
                status=validation_run.status,
                error="Validation run is not in a state that allows execution.",
            )
            return result

        # Mark running
        validation_run.status = ValidationRunStatus.RUNNING
        if not validation_run.started_at:
            validation_run.started_at = timezone.now()

        validation_run.save(update_fields=["status", "started_at"])

        # Try to run each step in the workflow
        # We don't need an execution plan we can just run the steps in order.
        workflow: Workflow = validation_run.workflow
        try:
            step_summaries = []
            steps = workflow.steps.all().order_by("order")
            for wf_step in steps:
                self.execute_step(wf_step, validation_run)  # implement logic inside
                step_summaries.append(
                    {
                        "step_id": wf_step.id,
                        "name": getattr(wf_step, "name", str(wf_step)),
                    },
                )
        except Exception as exc:  # noqa: BLE001
            validation_run.status = ValidationRunStatus.FAILED
            if hasattr(validation_run, "ended_at"):
                validation_run.ended_at = timezone.now()
            if hasattr(validation_run, "error"):
                validation_run.error = str(exc)
            validation_run.save()
            return ValidationRunTaskResult(
                run_id=validation_run.id, status=validation_run.status, error=str(exc)
            )

        result = {
            "summary": f"Executed {len(step_summaries)} step(s) for workflow {workflow.id}.",
            "steps": step_summaries,
        }
        validation_run.status = ValidationRunStatus.SUCCEEDED
        if hasattr(validation_run, "ended_at"):
            validation_run.ended_at = timezone.now()
        if hasattr(validation_run, "summary"):
            validation_run.summary = result.get("summary", "")
        if hasattr(validation_run, "result"):
            validation_run.result = result
        validation_run.save()
        return ValidationRunTaskResult(
            run_id=validation_run.id, status=validation_run.status, result=result
        )

    def execute_step(
        self,
        step: WorkflowStep,
        validation_run: ValidationRun,
    ):
        pass
