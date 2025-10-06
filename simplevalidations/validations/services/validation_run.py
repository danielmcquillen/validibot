from __future__ import annotations

import contextlib
import logging
from dataclasses import asdict
from typing import TYPE_CHECKING
from typing import Any

from celery.exceptions import TimeoutError as CeleryTimeout
from django.conf import settings
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.response import Response

from simplevalidations.validations.constants import StepStatus
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.engines.registry import get as get_validator_class
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.serializers import ValidationRunSerializer
from simplevalidations.validations.services.models import ValidationRunSummary
from simplevalidations.validations.services.models import ValidationRunTaskResult
from simplevalidations.validations.services.models import ValidationStepSummary

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from simplevalidations.submissions.models import Submission
    from simplevalidations.users.models import Organization
    from simplevalidations.validations.engines.base import BaseValidatorEngine
    from simplevalidations.validations.engines.base import ValidationResult
    from simplevalidations.validations.models import Ruleset
    from simplevalidations.validations.models import Validator
    from simplevalidations.workflows.models import Workflow
    from simplevalidations.workflows.models import WorkflowStep


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
        from simplevalidations.validations.tasks import (  # noqa:PLC0415
            execute_validation_run,
        )

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
                "user_id": request.user.id,
                "metadata": metadata or {},
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
                "api:validation-runs-detail",
                kwargs={"pk": validation_run.id},
            ),
        )

        data = ValidationRunSerializer(validation_run).data
        if validation_run.status in terminal_statuses:
            # Finished (either success or failure)
            response = Response(
                data,
                status=status.HTTP_201_CREATED,
                headers={"Location": location},
            )
        else:
            # Still running or pending
            # Add the URL to poll for status
            data["url"] = location
            data["poll"] = location
            headers = {"Location": location, "Retry-After": str(per_attempt)}
            response = Response(
                data,
                status=status.HTTP_202_ACCEPTED,
                headers=headers,
            )

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
            ValidationRunSummary: The result of the validation run execution.
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
            result = ValidationRunSummary(
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
        overall_failed = False
        step_summaries = []
        try:
            workflow_steps = workflow.steps.all().order_by("order")
            for wf_step in workflow_steps:
                validation_result: ValidationResult = self.execute_workflow_step(
                    step=wf_step,
                    validation_run=validation_run,
                )
                step_summary: ValidationStepSummary = ValidationStepSummary(
                    step_id=wf_step.id,
                    name=wf_step.name,
                    status=(
                        StepStatus.PASSED
                        if validation_result.passed
                        else StepStatus.FAILED
                    ),
                    issues=validation_result.issues or [],
                )
                step_summaries.append(step_summary)
                if not validation_result.passed:
                    overall_failed = True
                    # For now we stop on first failure
                    break
        except Exception as exc:
            logger.exception("Validation run execution failed: %s", exc)
            validation_run.status = ValidationRunStatus.FAILED
            if hasattr(validation_run, "ended_at"):
                validation_run.ended_at = timezone.now()
            if hasattr(validation_run, "error"):
                validation_run.error = str(exc)
            validation_run.save()
            return ValidationRunTaskResult(
                run_id=validation_run.id,
                status=validation_run.status,
                error="Validation run execution failed.",
            )

        validation_run_summary: ValidationRunSummary = ValidationRunSummary(
            overview=f"Executed {len(step_summaries)} step(s) for workflow {workflow.id}.",  # noqa: E501
            steps=step_summaries,
        )

        # Update the ValidationRun instance...
        if overall_failed:
            validation_run.status = ValidationRunStatus.FAILED
            validation_run.error = _("One or more validation steps failed.")
        else:
            validation_run.status = ValidationRunStatus.SUCCEEDED
            validation_run.error = ""
        validation_run.ended_at = timezone.now()
        # This will be redundant in the future because we will store information
        # at the step level later, but for now we are only logging step information
        # so we store the entire summary on the run here.
        validation_run.summary = asdict(validation_run_summary)
        validation_run.save(
            update_fields=[
                "status",
                "error",
                "ended_at",
                "summary",
            ]
        )

        # Create a ValidationRunTaskResult to return
        # to whoever called this execute() method.
        # Note that the real data we're concerned about is stored in the
        # ValidationRun DB record; this result is just a convenience.
        # The user of an API will get the data from the DB record.
        result = ValidationRunTaskResult(
            run_id=validation_run.id,
            status=validation_run.status,
            summary=validation_run_summary,
            error=validation_run.error,
        )

        return result

    def execute_workflow_step(
        self,
        step: WorkflowStep,
        validation_run: ValidationRun,
    ) -> ValidationResult:
        """
        Execute a single workflow step against the ValidationRun's submission.

        This simple implementation:
          - Resolves the workflow step's Validator and optional Ruleset.
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
        step_config = getattr(step, "config", {}) or {}
        validation_result: ValidationResult = self.run_validator_engine(
            validator=validator,
            submission=submission,
            ruleset=ruleset,
            config=step_config,
        )
        validation_result.workflow_step_name = step.name

        # TODO:
        #  Later when we support multiple steps per run, we will persist
        #  a ValidationStepRun record here with the result, timings, etc.
        #  For now, we just log the outcome.

        # 4) For this minimal implementation, just log the outcome.
        # The outer execute() method already handles overall run status and summary.
        issue_count = len(getattr(validation_result, "issues", []) or [])
        passed = getattr(validation_result, "passed", False)

        logger.info(
            "Validation step executed: workflow_step_id=%s validator=%s issues=%s passed=%s",  # noqa: E501
            getattr(step, "id", None),
            getattr(validator, "validation_type", None),
            issue_count,
            passed,
        )
        # If you want to persist per-step outputs later, this is where you'd create
        # a ValidationRunStep row and store result.to_dict(), timings,

        return validation_result

    def run_validator_engine(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset | None = None,
        config: dict[str, Any] | None = None,
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

        # TODO: Later we might want to store a default config in a Validator and pass
        # to the engine. For now we just pass an empty dict.
        config = config or {}

        try:
            validator_cls = get_validator_class(vtype)
        except Exception as exc:
            err_msg = f"Failed to load validator engine for type '{vtype}': {exc}"
            raise ValueError(err_msg) from exc

        # To keep validator engine classes clean, we pass everything it
        # needs either via the config dict or the ContentSource.
        # We don't pass in any model instances.
        validator_engine: BaseValidatorEngine = validator_cls(config=config)
        validation_result = validator_engine.validate(
            validator=validator,
            submission=submission,
            ruleset=ruleset,
        )

        return validation_result
