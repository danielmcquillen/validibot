"""
Validation run service — public facade for creating and managing validation runs.

This module is the main entry point for validation run lifecycle management.
It provides two web-layer operations (launch, cancel) and delegates worker-side
execution to the StepOrchestrator. Summary building is delegated to the
SummaryBuilder module.

Architecture:

    ValidationRunService (this file)
        ├── launch()                  — web-side: create run, dispatch to worker
        ├── cancel_run()              — web-side: cancel a pending/running run
        ├── execute_workflow_steps()   → delegates to StepOrchestrator
        └── rebuild_run_summary_record() → delegates to summary_builder

    StepOrchestrator (step_orchestrator.py)
        ├── execute_workflow_steps()   — worker-side: iterate steps
        ├── execute_workflow_step()    — dispatch single step to handler
        └── (step lifecycle, result recording, signal extraction)

    SummaryBuilder (summary_builder.py)
        ├── build_run_summary_record() — build summaries from DB findings
        └── rebuild_run_summary_record() — idempotent public entry point

    FindingsPersistence (findings_persistence.py)
        ├── normalize_issue()          — coerce raw issues to ValidationIssue
        ├── persist_findings()         — bulk-create ValidationFinding rows
        └── (severity coercion helpers)

See Also:
    - docs/dev_docs/overview/service_architecture.md
    - GitHub issue #95: Split ValidationRunService into focused modules
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING
from typing import Any

from attr import dataclass
from attr import field
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework import status

from validibot.submissions.constants import get_output_retention_timedelta
from validibot.tracking.services import TrackingEventService
from validibot.validations.constants import ValidationRunSource
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationRunSummary
from validibot.validations.services.step_orchestrator import StepOrchestrator

logger = logging.getLogger(__name__)

GENERIC_EXECUTION_ERROR = _(
    "This validation run could not be completed. Please try again later.",
)

RUN_CANCELED_MESSAGE = _("Run canceled by user.")

if TYPE_CHECKING:
    from uuid import UUID

    from validibot.submissions.models import Submission
    from validibot.users.models import Organization
    from validibot.users.models import User
    from validibot.validations.services.models import ValidationRunTaskResult
    from validibot.workflows.models import Workflow


@dataclass
class ValidationRunLaunchResults:
    validation_run: ValidationRun
    data: dict[str, Any] = field(factory=dict)
    status: int | None = None


class ValidationRunService:
    """
    Public facade for validation run lifecycle management.

    This is the stable API that views, API endpoints, task queues, and callback
    handlers use. It handles run creation (launch) and cancellation directly,
    and delegates step execution and summary building to focused internal
    modules.

    Internal orchestration is handled by:
    - StepOrchestrator: Step iteration, dispatch, and result recording
    - SummaryBuilder: Run/step summary aggregation from DB findings
    - FindingsPersistence: Issue normalization and finding persistence

    Main entry points:

        launch(request, org, workflow, submission, ...)
            Creates a ValidationRun and dispatches execution. Called by views/API.

        execute_workflow_steps(validation_run_id, user_id)
            Processes workflow steps sequentially. Called by task queue.

        cancel_run(run, actor)
            Cancels a run that hasn't completed yet.

        rebuild_run_summary_record(validation_run)
            Rebuilds summary records from persisted findings. Called by
            async validator callback handler.

    See Also:
        - StepOrchestrator: Worker-side step execution
        - SummaryBuilder: Summary aggregation
        - FindingsPersistence: Issue normalization and finding creation
    """

    def __init__(self) -> None:
        self._orchestrator = StepOrchestrator()

    # ---------- Launch (views call this) ----------

    def launch(
        self,
        request,
        org: Organization,
        workflow: Workflow,
        submission: Submission,
        user_id: int,
        metadata: dict | None = None,
        *,
        extra: dict | None = None,
        source: ValidationRunSource = ValidationRunSource.LAUNCH_PAGE,
    ) -> ValidationRunLaunchResults:
        """
        Create a ValidationRun and dispatch execution.

        This is the web-layer entry point called by views and API endpoints.
        It validates preconditions, creates the run record, and dispatches
        execution to the appropriate backend (Celery, Cloud Tasks, etc.).

        Args:
            request: The HTTP request object (for user auth and URI building).
            org: The organization under which the run is created.
            workflow: The workflow to execute.
            submission: The file/content to validate.
            user_id: ID of the user initiating the run.
            metadata: Optional metadata to associate with the run.
            extra: Additional fields to pass to ValidationRun.objects.create().
            source: Origin of the run (LAUNCH_PAGE, API, etc.).

        Returns:
            ValidationRunLaunchResults with the run and HTTP status code:
            - 201 Created if execution completed (SUCCEEDED, FAILED, CANCELED)
            - 202 Accepted if still processing (PENDING, RUNNING)

        Raises:
            ValueError: If required arguments are missing.
            PermissionError: If user lacks execute permission on workflow.
        """
        from validibot.core.tasks import enqueue_validation_run

        start_time = time.perf_counter()
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

        run_user = None
        if getattr(submission, "user_id", None):
            run_user = submission.user
        elif getattr(request.user, "is_authenticated", False):
            run_user = request.user

        # Compute output expiry based on workflow's output retention policy
        output_retention_policy = workflow.output_retention
        output_expires_at = None
        retention_delta = get_output_retention_timedelta(output_retention_policy)
        if retention_delta is not None:
            # Note: expiry is computed from now, not from run completion.
            # This is simpler and adequate since runs typically complete quickly.
            # For very long runs, the expiry will be slightly earlier than
            # "completion + retention period", which is acceptable.
            output_expires_at = timezone.now() + retention_delta

        with transaction.atomic():
            validation_run = ValidationRun.objects.create(
                org=org,
                workflow=workflow,
                submission=submission,
                project=getattr(submission, "project", None)
                or getattr(workflow, "project", None),
                user=run_user,
                status=ValidationRunStatus.PENDING,
                source=source,
                output_retention_policy=output_retention_policy,
                output_expires_at=output_expires_at,
                **(extra or {}),
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

            tracking_service = TrackingEventService()
            created_extra: dict[str, Any] = {}
            if metadata:
                created_extra["metadata_keys"] = sorted(metadata.keys())
            tracking_service.log_validation_run_created(
                run=validation_run,
                user=run_user,
                submission_id=submission.pk,
                extra_data=created_extra or None,
            )

        # Dispatch execution to the appropriate backend:
        # - Test: Synchronous inline execution
        # - Local dev: HTTP call to worker
        # - Docker Compose: Celery task queue
        # - GCP: Cloud Tasks
        # - AWS: TBD (future)
        try:
            enqueue_validation_run(
                validation_run_id=validation_run.id,
                user_id=request.user.id,
            )
        except Exception:
            logger.exception(
                "Failed to enqueue validation run %s",
                validation_run.id,
            )
            validation_run.status = ValidationRunStatus.FAILED
            validation_run.error = GENERIC_EXECUTION_ERROR
            validation_run.save(update_fields=["status", "error"])

        # Refresh from DB to get any updates made during execution
        # This is primarily for test mode where execute_workflow_steps() runs
        # synchronously,
        # but also provides correct status if execution completed very quickly
        validation_run.refresh_from_db()

        # Return appropriate HTTP status based on run state:
        # - 201 Created if execution completed (SUCCEEDED, FAILED, CANCELED)
        # - 202 Accepted if still processing (PENDING, RUNNING)
        if validation_run.status in {
            ValidationRunStatus.SUCCEEDED,
            ValidationRunStatus.FAILED,
            ValidationRunStatus.CANCELED,
        }:
            http_status = status.HTTP_201_CREATED
        else:
            http_status = status.HTTP_202_ACCEPTED

        results: ValidationRunLaunchResults = ValidationRunLaunchResults(
            validation_run=validation_run,
            status=http_status,
        )

        logger.info(
            "Validation run %s launch completed in %.2f ms (status=%s, enqueued)",
            validation_run.id,
            (time.perf_counter() - start_time) * 1000,
            validation_run.status,
        )
        return results

    # ---------- Cancel ----------

    def cancel_run(
        self,
        *,
        run: ValidationRun,
        actor: User | None = None,
    ) -> tuple[ValidationRun, bool]:
        """Attempt to cancel a validation run if it has not finished yet."""

        if run is None:
            raise ValueError("run is required to cancel a validation")

        run.refresh_from_db()
        if run.status == ValidationRunStatus.CANCELED:
            return run, True

        if run.status not in (
            ValidationRunStatus.PENDING,
            ValidationRunStatus.RUNNING,
        ):
            return run, False

        run.status = ValidationRunStatus.CANCELED
        if not run.ended_at:
            run.ended_at = timezone.now()
        if not run.error:
            run.error = RUN_CANCELED_MESSAGE
        run.save(update_fields=["status", "ended_at", "error"])

        tracking_service = TrackingEventService()
        extra = {"duration_ms": run.computed_duration_ms}
        tracking_service.log_validation_run_status(
            run=run,
            status=ValidationRunStatus.CANCELED,
            actor=actor,
            extra_data=extra,
        )

        return run, True

    # ---------- Delegated to StepOrchestrator ----------

    def execute_workflow_steps(
        self,
        validation_run_id: UUID | str,
        user_id: int | None,
        resume_from_step: int | None = None,
    ) -> ValidationRunTaskResult:
        """Process workflow steps for a ValidationRun.

        Delegates to StepOrchestrator. See StepOrchestrator.execute_workflow_steps
        for full documentation.
        """
        return self._orchestrator.execute_workflow_steps(
            validation_run_id=validation_run_id,
            user_id=user_id,
            resume_from_step=resume_from_step,
        )

    def execute_workflow_step(self, step, validation_run):
        """Dispatch a single workflow step to its handler.

        Delegates to StepOrchestrator. See StepOrchestrator.execute_workflow_step
        for full documentation.
        """
        return self._orchestrator.execute_workflow_step(
            step=step,
            validation_run=validation_run,
        )

    # ---------- Delegated to SummaryBuilder ----------

    def rebuild_run_summary_record(
        self,
        *,
        validation_run: ValidationRun,
    ) -> ValidationRunSummary:
        """Rebuild run and step summary records from persisted state.

        Delegates to summary_builder.rebuild_run_summary_record.
        """
        from validibot.validations.services.summary_builder import (
            rebuild_run_summary_record,
        )

        return rebuild_run_summary_record(validation_run=validation_run)
