from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from celery.exceptions import TimeoutError as CeleryTimeout
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.response import Response

from roscoe.validations.constants import ValidationRunStatus
from roscoe.validations.engines.registry import get as get_validator_class
from roscoe.validations.models import ValidationRun

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from roscoe.submissions.models import Submission
    from roscoe.users.models import Organization
    from roscoe.validations.engines.base import BaseValidatorEngine
    from roscoe.validations.engines.base import ValidationResult
    from roscoe.validations.models import Ruleset
    from roscoe.validations.models import Validator
    from roscoe.workflows.models import Workflow
    from roscoe.workflows.models import WorkflowStep


@dataclass
class ValidationRunTaskPayload:
    content: dict[str, Any] | None = None
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

    def launch(  # noqa: PLR0913
        self,
        request,
        org: Organization,
        workflow: Workflow,
        submission: Submission,
        user_id: int,
        metadata: dict | None = None,
    ) -> Response:
        """
        Creates a validation run for a given workflow and a user request.
        The user should have provided us with a 'submission' as part of their request.
        The submission is the content (json, xml, whatever) that the workflow will
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
            user_id:        The ID of the user initiating the run.
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

        for _index in range(attempts):
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
            workflow_steps = workflow.steps.all().order_by("order")
            for wf_step in workflow_steps:
                self.execute_workflow_step(
                    step=wf_step,
                    validation_run=validation_run,
                )  # implement logic inside
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
            run_id=validation_run.id,
            status=validation_run.status,
            result=result,
        )

    def execute_workflow_step(
        self,
        step: WorkflowStep,
        validation_run: ValidationRun,
    ) -> bool:
        """
        Execute a single workflow step against the run's submission.

        This simple implementation:
          - Resolves the step's Validator and optional Ruleset.
          - Resolves the Submission from the ValidationRun.
          - Calls the validator runner, which routes to the correct engine via registry.
          - Logs the result; does not (yet) persist per-step outputs.

        Any exception raised here will be caught by the caller (execute()), which will
        mark the ValidationRun as FAILED.

        NOTE: For this minimal implementation, we just log the outcome of each step.
        The calling execute() method already handles overall run status and summary.

        Args:
            step (WorkflowStep): The workflow step to execute.
            validation_run (ValidationRun): The validation run context.

        Raises:
            ValueError: If the step has no validator configured.

        Returns:
            Just a boolean True for now; could be extended to return more info.

        """
        # 1) Resolve engine inputs from the step
        validator = getattr(step, "validator", None)
        if validator is None:
            raise ValueError(_("WorkflowStep has no validator configured."))
        # It may be that no ruleset is configured, in which case the validator
        # implementation should use its default or built-in rules.
        ruleset = getattr(step, "ruleset", None)

        # 2) Materialize submission content as text
        submission: Submission = validation_run.submission

        # 3) Run the validator (registry resolves the concrete class by type/variant)
        result = self.run_validator_engine(
            validator=validator,
            submission=submission,
            ruleset=ruleset,
        )

        # 4) For this minimal implementation, just log the outcome.
        # The outer execute() method already handles overall run status and summary.
        issue_count = len(getattr(result, "issues", []) or [])
        logger.info(
            "Validation step executed: workflow_step_id=%s validator=%s issues=%s passed=%s",  # noqa: E501
            getattr(step, "id", None),
            getattr(validator, "validation_type", None),
            issue_count,
            getattr(result, "passed", False),
        )
        # If you want to persist per-step outputs later, this is where you'd create
        # a ValidationRunStep row and store result.to_dict(), timings,

        return True

    def run_validator_engine(
        *,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset | None = None,
    ) -> ValidationResult:
        """
        Execute the appropriate validator engine code for the given
        Validator model, the Submission from the user, and optional Ruleset.

        Args:
            validator (Validator): The Validator model instance to use.
            submission (Submission): The Submission containing the content to validate.
            ruleset (Ruleset, optional): An optional Ruleset to apply during validation.

        Returns:
            ValidationResult: The result of the validation, including issues found.

        """
        vtype = validator.validation_type
        if not vtype:
            raise ValueError(_("Validator model missing 'type' or 'validation_type'."))

        config: dict[str, Any] = validator.config or {}
        validator_cls = get_validator_class(vtype)

        # To keep validator engine classes clean, we pass everything it
        # needs either via the config dict or the ContentSource.
        # We don't pass in any model instances.
        validator_engine: BaseValidatorEngine = validator_cls(config=config)
        return validator_engine.validate(
            validator=validator,
            source=submission,
            ruleset=ruleset,
        )
