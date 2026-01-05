from __future__ import annotations

import logging
import time
from collections import Counter
from typing import TYPE_CHECKING
from typing import Any

from attr import dataclass
from attr import field
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework import status

from validibot.tracking.services import TrackingEventService
from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunErrorCategory
from validibot.validations.constants import ValidationRunSource
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.engines.base import ValidationIssue
from validibot.validations.engines.base import ValidationResult
from validibot.validations.models import ValidationFinding
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationRunSummary
from validibot.validations.models import ValidationStepRun
from validibot.validations.models import ValidationStepRunSummary
from validibot.validations.services.models import ValidationRunTaskResult

logger = logging.getLogger(__name__)

# Billing enforcement is checked before creating a validation run.
# If the org has exceeded their limits or trial has expired, the launch fails.
# These imports are here to avoid circular dependencies.
BILLING_ENFORCEMENT_ENABLED = True  # Feature flag for gradual rollout

GENERIC_EXECUTION_ERROR = _(
    "This validation run could not be completed. Please try again later.",
)

RUN_CANCELED_MESSAGE = _("Run canceled by user.")

if TYPE_CHECKING:
    from validibot.submissions.models import Submission
    from validibot.users.models import Organization
    from validibot.users.models import User
    from validibot.workflows.models import Workflow
    from validibot.workflows.models import WorkflowStep


@dataclass
class ValidationRunLaunchResults:
    validation_run: ValidationRun
    data: dict[str, Any] = field(factory=dict)
    status: int | None = None


class ValidationRunService:
    """
    Orchestrates the complete lifecycle of validation runs.

    This is the central service for creating, executing, and tracking validation
    runs. It coordinates between the workflow engine, validator engines, action
    handlers, and persistence layer.

    Main entry points:

        launch(request, org, workflow, submission, ...)
            Creates a ValidationRun and begins execution. Called by views/API.

        execute(validation_run_id, user_id, metadata)
            Processes workflow steps sequentially. Called by launch() or can
            be invoked directly to resume a run.

        cancel_run(run, actor)
            Cancels a run that hasn't completed yet.

    Execution model:

        - Sync validators (Basic, JSON, XML, AI) execute inline and return
          immediately with passed=True/False.

        - Async validators (EnergyPlus, FMI) launch Cloud Run Jobs and return
          passed=None (pending). The workflow pauses and resumes when the
          job callback arrives.

        - Action handlers (Slack, Certificate) are dispatched via the action
          registry and follow the same StepHandler protocol.

    State transitions:

        PENDING → RUNNING → SUCCEEDED | FAILED | CANCELED

        For async validators, the run stays in RUNNING until the callback
        processing finalizes the result.

    See Also:
        - ValidatorStepHandler: Bridges workflow engine to validator engines
        - BaseValidatorEngine: Abstract base for all validation engines
        - StepHandler protocol: Interface for step execution
    """

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
        Create a ValidationRun and enqueue execution via Cloud Tasks.

        This is the main entry point called by views and API endpoints. It:

        1. Validates preconditions (permissions, billing limits)
        2. Creates a ValidationRun record with status=PENDING
        3. Enqueues a Cloud Task to execute the workflow steps
        4. Returns immediately with appropriate HTTP status

        Execution happens asynchronously on the worker instance. The run status
        will transition to RUNNING, then to SUCCEEDED/FAILED when complete.
        Clients should poll for completion or use webhooks.

        Args:
            request: The HTTP request object (used for user auth and URI building).
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
            BillingError: If org has exceeded limits or subscription is inactive.

        See Also:
            ADR-001: Validation Run Execution via Cloud Tasks
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

        # Check billing limits before proceeding
        # For workflow guests, billing is attributed to the workflow owner's org
        if BILLING_ENFORCEMENT_ENABLED:
            self._check_billing_limits(org, workflow, user=request.user)

        run_user = None
        if getattr(submission, "user_id", None):
            run_user = submission.user
        elif getattr(request.user, "is_authenticated", False):
            run_user = request.user

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

        # Enqueue execution via Cloud Tasks (async)
        # The worker will call execute() when it receives the task
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
        # This is primarily for test mode where execute() runs synchronously,
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

        if run.status not in (ValidationRunStatus.PENDING, ValidationRunStatus.RUNNING):
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

    # ---------- Execute ----------

    def execute(
        self,
        validation_run_id: int,
        user_id: int,
        metadata: dict | None = None,
        resume_from_step: int | None = None,
    ) -> ValidationRunTaskResult:
        """
        Process workflow steps for a ValidationRun.

        Iterates through the workflow's steps in order, dispatching each to the
        appropriate handler (ValidatorStepHandler for validators, or an action
        handler from the registry). Execution stops on first failure or when
        an async validator returns pending.

        For sync validators, all steps execute inline and the run reaches a
        terminal status (SUCCEEDED/FAILED) before returning.

        For async validators (EnergyPlus, FMI), this method returns while the
        run is still RUNNING. When the Cloud Run Job callback arrives, it
        enqueues a new Cloud Task with resume_from_step to continue execution.

        Idempotency:
            - Initial execution (resume_from_step=None): Only proceeds if status
              is PENDING. Transitions to RUNNING atomically.
            - Resume execution (resume_from_step set): Expects status to be RUNNING.
              No state transition needed.
            - Cloud Tasks may deliver the same task multiple times. Step-level
              idempotency is handled by _start_step_run() using get_or_create.

        Args:
            validation_run_id: ID of the ValidationRun to execute.
            user_id: ID of the user who initiated the run (for tracking).
            metadata: Optional metadata passed through to step handlers.
            resume_from_step: Step order to resume from (None for initial execution).

        Returns:
            ValidationRunTaskResult with the final (or current) run status.

        See Also:
            ADR-001: Validation Run Execution via Cloud Tasks
        """
        try:
            validation_run: ValidationRun = ValidationRun.objects.select_related(
                "workflow",
                "org",
                "project",
                "submission",
            ).get(id=validation_run_id)
        except ValidationRun.DoesNotExist:
            logger.exception(
                "ValidationRun %s missing when execution task started.",
                validation_run_id,
            )
            return ValidationRunTaskResult(
                run_id=validation_run_id,
                status=ValidationRunStatus.FAILED,
                error=GENERIC_EXECUTION_ERROR,
            )

        tracking_service = TrackingEventService()
        actor = self._resolve_run_actor(validation_run, user_id)

        # Idempotency check and state transition based on entry point
        # See ADR-001 for detailed explanation
        if resume_from_step is None:
            # Initial execution: atomically transition PENDING → RUNNING
            # This prevents race conditions when Cloud Tasks delivers duplicates
            now = timezone.now()
            updated_count = ValidationRun.objects.filter(
                id=validation_run_id,
                status=ValidationRunStatus.PENDING,
            ).update(
                status=ValidationRunStatus.RUNNING,
                started_at=now,
            )

            if updated_count == 0:
                # Either already started or status changed - fetch current state
                validation_run.refresh_from_db()
                logger.info(
                    "Validation run %s already started (status=%s), skipping",
                    validation_run_id,
                    validation_run.status,
                )
                return ValidationRunTaskResult(
                    run_id=validation_run.id,
                    status=validation_run.status,
                    error="",
                )

            # Update local object to reflect DB state
            validation_run.status = ValidationRunStatus.RUNNING
            validation_run.started_at = now

            tracking_service.log_validation_run_started(
                run=validation_run,
                user=actor,
                extra_data={"status": ValidationRunStatus.RUNNING},
            )
        elif validation_run.status != ValidationRunStatus.RUNNING:
            # Resume from callback: expect status to be RUNNING
            logger.warning(
                "Validation run %s not RUNNING for resume (status=%s), skipping",
                validation_run_id,
                validation_run.status,
            )
            return ValidationRunTaskResult(
                run_id=validation_run.id,
                status=validation_run.status,
                error=_("Validation run is not in a state that allows execution."),
            )

        def _was_cancelled() -> bool:
            validation_run.refresh_from_db(fields=["status"])
            return validation_run.status == ValidationRunStatus.CANCELED

        workflow: Workflow = validation_run.workflow
        overall_failed = False
        pending_async = False
        failing_step_id = None
        cancelled = False
        step_metrics: list[dict[str, Any]] = []

        try:
            workflow_steps = workflow.steps.all().order_by("order")
            # Filter steps for resume execution
            if resume_from_step is not None:
                workflow_steps = workflow_steps.filter(order__gte=resume_from_step)

            for wf_step in workflow_steps:
                if _was_cancelled():
                    cancelled = True
                    break
                step_run, should_execute = self._start_step_run(
                    validation_run=validation_run,
                    workflow_step=wf_step,
                )

                # Skip already-completed steps (idempotency on retry)
                if not should_execute:
                    # If the step failed, we should stop (same as normal failure)
                    if step_run.status == StepStatus.FAILED:
                        overall_failed = True
                        failing_step_id = wf_step.id
                        break
                    # Otherwise, continue to the next step
                    continue

                try:
                    validation_result: ValidationResult = self.execute_workflow_step(
                        step=wf_step,
                        validation_run=validation_run,
                    )
                except Exception as exc:
                    self._finalize_step_run(
                        step_run=step_run,
                        status=StepStatus.FAILED,
                        stats=None,
                        error=str(exc),
                    )
                    step_metrics.append(
                        {
                            "step_run": step_run,
                            "severity_counts": Counter(),
                            "total_findings": 0,
                            "assertion_failures": 0,
                            "assertion_total": 0,
                        },
                    )
                    raise
                metrics = self._record_step_result(
                    validation_run=validation_run,
                    step_run=step_run,
                    validation_result=validation_result,
                )
                step_metrics.append(metrics)
                if validation_result.passed is False:
                    overall_failed = True
                    failing_step_id = wf_step.id
                    # For now we stop on first failure.
                    break
                if validation_result.passed is None:
                    # Async validator in progress; pause the workflow here and wait
                    # for callback processing to finalize run/step state.
                    pending_async = True
                    break
                if _was_cancelled():
                    cancelled = True
                    break
        except Exception as exc:
            logger.exception("Validation run execution failed")
            validation_run.status = ValidationRunStatus.FAILED
            validation_run.ended_at = timezone.now()
            validation_run.error = GENERIC_EXECUTION_ERROR
            validation_run.error_category = ValidationRunErrorCategory.RUNTIME_ERROR
            validation_run.summary = {}
            validation_run.save(
                update_fields=[
                    "status",
                    "ended_at",
                    "error",
                    "error_category",
                    "summary",
                ],
            )
            tracking_service.log_validation_run_status(
                run=validation_run,
                status=ValidationRunStatus.FAILED,
                actor=actor,
                extra_data={"exception": str(exc)},
            )
            self._build_run_summary_record(
                validation_run=validation_run,
                step_metrics=step_metrics,
            )
            return ValidationRunTaskResult(
                run_id=validation_run.id,
                status=validation_run.status,
                error=GENERIC_EXECUTION_ERROR,
            )

        if cancelled or _was_cancelled():
            validation_run.status = ValidationRunStatus.CANCELED
            validation_run.error = validation_run.error or RUN_CANCELED_MESSAGE
            if not validation_run.ended_at:
                validation_run.ended_at = timezone.now()
            validation_run.summary = {}
            validation_run.save(
                update_fields=["status", "error", "ended_at", "summary"],
            )
            summary_record = self._build_run_summary_record(
                validation_run=validation_run,
                step_metrics=step_metrics,
            )
            extra_payload = {
                "step_count": len(step_metrics),
                "finding_count": summary_record.total_findings if summary_record else 0,
                "duration_ms": validation_run.computed_duration_ms,
            }
            tracking_service.log_validation_run_status(
                run=validation_run,
                status=ValidationRunStatus.CANCELED,
                actor=actor,
                extra_data=extra_payload,
            )
            return ValidationRunTaskResult(
                run_id=validation_run.id,
                status=ValidationRunStatus.CANCELED,
                error=validation_run.error,
            )

        if pending_async:
            # Leave run/step in RUNNING state. Callback processing will finalize
            # statuses, findings, summaries, and end timestamps.
            return ValidationRunTaskResult(
                run_id=validation_run.id,
                status=validation_run.status,
                error="",
            )

        if overall_failed:
            validation_run.status = ValidationRunStatus.FAILED
            validation_run.error = _("One or more validation steps failed.")
            validation_run.error_category = ValidationRunErrorCategory.VALIDATION_FAILED
        else:
            validation_run.status = ValidationRunStatus.SUCCEEDED
            validation_run.error = ""
            validation_run.error_category = ""
        validation_run.ended_at = timezone.now()
        validation_run.summary = {}
        validation_run.save(
            update_fields=[
                "status",
                "error",
                "error_category",
                "ended_at",
                "summary",
            ],
        )

        summary_record = self._build_run_summary_record(
            validation_run=validation_run,
            step_metrics=step_metrics,
        )

        result = ValidationRunTaskResult(
            run_id=validation_run.id,
            status=validation_run.status,
            error=validation_run.error,
        )
        completion_extra: dict[str, Any] = {
            "step_count": len(step_metrics),
            "failing_step_id": failing_step_id,
            "finding_count": summary_record.total_findings if summary_record else 0,
        }
        extra_payload = {
            **{k: v for k, v in completion_extra.items() if v is not None},
            "duration_ms": validation_run.computed_duration_ms,
        }
        tracking_service.log_validation_run_status(
            run=validation_run,
            status=validation_run.status,
            actor=actor,
            extra_data=extra_payload,
        )

        return result

    # ---------- Private methods ----------

    def _check_billing_limits(
        self,
        org: Organization,
        workflow: Workflow,
        user: User | None = None,
    ) -> None:
        """
        Check billing limits before creating a validation run.

        For basic workflows: increments the usage counter and checks monthly limit.
        For advanced workflows: checks credit balance.

        For workflow guests (users with grants but no org membership), billing is
        attributed to the workflow owner's organization, not the user's org.

        Args:
            org: The organization passed to launch (may be None for guests).
            workflow: The workflow being executed.
            user: The user executing the workflow (used to check guest status).

        Raises:
            BillingError (or subclass) if limits exceeded or subscription inactive.
        """
        # Local imports to avoid circular dependencies
        from validibot.billing.metering import AdvancedWorkflowMeter
        from validibot.billing.metering import BasicWorkflowMeter

        # Determine billing org: for guests, use workflow's org
        billing_org = org
        if user and getattr(user, "is_workflow_guest", False):
            # Workflow guests have their usage billed to the workflow owner's org
            billing_org = workflow.org
            logger.info(
                "Guest user %s billing attributed to workflow org %s",
                user.id,
                billing_org.id,
            )

        # Check if billing org has a subscription (it should, but handle edge cases)
        if not hasattr(billing_org, "subscription"):
            logger.warning(
                "Organization %s has no subscription, skipping billing check",
                billing_org.id,
            )
            return

        # Determine if workflow is advanced (uses high-compute validators)
        is_advanced = getattr(workflow, "is_advanced", False)

        if is_advanced:
            # For advanced workflows, check credit balance
            # Credit deduction happens after the run completes
            AdvancedWorkflowMeter().check_can_launch(billing_org, credits_required=1)
        else:
            # For basic workflows, check and increment usage counter
            BasicWorkflowMeter().check_and_increment(billing_org)

    def _start_step_run(
        self,
        *,
        validation_run: ValidationRun,
        workflow_step: WorkflowStep,
    ) -> tuple[ValidationStepRun, bool]:
        """
        Get or create a ValidationStepRun entry for the step.

        This method is idempotent to handle Cloud Tasks retries. If a step run
        already exists for this (validation_run, workflow_step) pair, it returns
        the existing one. If the existing step run is already terminal (PASSED,
        FAILED, SKIPPED), the caller should skip re-execution.

        Returns:
            Tuple of (step_run, should_execute):
            - step_run: The ValidationStepRun instance
            - should_execute: True if the step should be executed, False if it
              should be skipped (already terminal or already RUNNING from a
              prior attempt)

        See Also:
            ADR-001: Idempotent Step Execution on Retry
        """
        with transaction.atomic():
            step_run, created = ValidationStepRun.objects.get_or_create(
                validation_run=validation_run,
                workflow_step=workflow_step,
                defaults={
                    "step_order": workflow_step.order or 0,
                    "status": StepStatus.RUNNING,
                    "started_at": timezone.now(),
                },
            )

            if not created:
                # Step run already exists - check if we should execute
                if step_run.status in {
                    StepStatus.PASSED,
                    StepStatus.FAILED,
                    StepStatus.SKIPPED,
                }:
                    # Already terminal - skip execution
                    logger.info(
                        "Step run %s already terminal (status=%s), skipping",
                        step_run.id,
                        step_run.status,
                    )
                    return step_run, False

                # Step is RUNNING - this is a retry. Clear any prior findings
                # to avoid duplicates, then re-execute.
                logger.info(
                    "Step run %s is RUNNING (retry scenario), clearing findings",
                    step_run.id,
                )
                ValidationFinding.objects.filter(
                    validation_step_run=step_run,
                ).delete()

            return step_run, True

    def _finalize_step_run(
        self,
        *,
        step_run: ValidationStepRun,
        status: StepStatus,
        stats: dict[str, Any] | None,
        error: str | None = None,
    ) -> ValidationStepRun:
        """Persist final status, duration, and diagnostics on a step run."""
        ended_at = timezone.now()
        step_run.status = status
        step_run.ended_at = ended_at
        if step_run.started_at:
            step_run.duration_ms = max(
                int((ended_at - step_run.started_at).total_seconds() * 1000),
                0,
            )
        else:
            step_run.duration_ms = 0
        step_run.output = stats or {}
        step_run.error = error or ""
        step_run.save(
            update_fields=[
                "status",
                "ended_at",
                "duration_ms",
                "output",
                "error",
            ],
        )
        return step_run

    def _normalize_issue(self, issue: Any) -> ValidationIssue:
        """Ensure every issue is a ValidationIssue dataclass."""
        if isinstance(issue, ValidationIssue):
            return issue
        if isinstance(issue, dict):
            severity = self._coerce_severity(issue.get("severity"))
            return ValidationIssue(
                path=str(issue.get("path", "") or ""),
                message=str(issue.get("message", "") or ""),
                severity=severity,
                code=str(issue.get("code", "") or ""),
                meta=issue.get("meta"),
                assertion_id=issue.get("assertion_id"),
            )
        return ValidationIssue(
            path="",
            message=str(issue),
            severity=Severity.ERROR,
        )

    def _coerce_severity(self, value: Any) -> Severity:
        """Convert arbitrary severity input to a Severity choice."""
        if isinstance(value, Severity):
            return value
        if isinstance(value, str):
            try:
                return Severity(value)
            except ValueError:
                pass
        return Severity.ERROR

    def _severity_value(self, value: Severity | str | None) -> str:
        """Return the string value that should be stored on ValidationFinding."""
        if isinstance(value, Severity):
            return value.value
        if isinstance(value, str) and value in Severity.values:
            return value
        return Severity.ERROR

    def _persist_findings(
        self,
        *,
        validation_run: ValidationRun,
        step_run: ValidationStepRun,
        issues: list[ValidationIssue],
    ) -> tuple[Counter, int]:
        severity_counts: Counter = Counter()
        assertion_failures = 0
        findings: list[ValidationFinding] = []
        for issue in issues:
            severity_value = self._severity_value(issue.severity)
            severity_counts[severity_value] += 1
            if issue.assertion_id:
                assertion_failures += 1
            meta = issue.meta or {}
            if meta and not isinstance(meta, dict):
                meta = {"detail": meta}
            finding = ValidationFinding(
                validation_run=validation_run,
                validation_step_run=step_run,
                severity=severity_value,
                code=issue.code or "",
                message=issue.message or "",
                path=issue.path or "",
                meta=meta,
                ruleset_assertion_id=issue.assertion_id,
            )
            finding._ensure_run_alignment()  # noqa: SLF001
            finding._strip_payload_prefix()  # noqa: SLF001
            findings.append(finding)
        if findings:
            ValidationFinding.objects.bulk_create(findings, batch_size=500)
        return severity_counts, assertion_failures

    def _record_step_result(
        self,
        *,
        validation_run: ValidationRun,
        step_run: ValidationStepRun,
        validation_result: ValidationResult,
    ) -> dict[str, Any]:
        """
        Persist step results and update run state.

        After a handler returns, this method:

        1. Normalizes issues and persists them as ValidationFinding rows.
        2. Extracts any "signals" from stats and stores them in run.summary
           for downstream CEL assertions to access.
        3. For sync results (passed=True/False): finalizes the step_run with
           status PASSED/FAILED.
        4. For async results (passed=None): keeps step_run as RUNNING.

        Returns a metrics dict used for building the run summary.
        """
        issues = [
            self._normalize_issue(issue) for issue in (validation_result.issues or [])
        ]
        severity_counts, assertion_failures = self._persist_findings(
            validation_run=validation_run,
            step_run=step_run,
            issues=issues,
        )
        stats = validation_result.stats or {}
        # Persist any signals for downstream steps in a namespaced structure.
        # Callers can include a "signals" dict in stats with catalog slugs/values.
        if "signals" in stats:
            summary_steps = validation_run.summary.get("steps", {})
            summary_steps[str(step_run.id)] = {
                "signals": stats.get("signals", {}),
            }
            validation_run.summary["steps"] = summary_steps
            validation_run.save(update_fields=["summary"])
        if validation_result.passed is None:
            # Async validator still running; keep status as RUNNING and persist
            # any interim stats for observability.
            step_run.output = stats
            step_run.status = StepStatus.RUNNING
            step_run.save(update_fields=["output", "status"])
            finalized_step = step_run
            status = StepStatus.RUNNING
        else:
            status = (
                StepStatus.PASSED if validation_result.passed else StepStatus.FAILED
            )
            finalized_step = self._finalize_step_run(
                step_run=step_run,
                status=status,
                stats=stats,
                error=None,
            )
        return {
            "step_run": finalized_step,
            "severity_counts": severity_counts,
            "total_findings": sum(severity_counts.values()),
            "assertion_failures": assertion_failures,
            "assertion_total": self._extract_assertion_total(stats),
        }

    def _extract_assertion_total(self, stats: dict[str, Any] | None) -> int:
        if not isinstance(stats, dict):
            return 0
        for key in ("assertion_count", "assertions_evaluated"):
            value = stats.get(key)
            if isinstance(value, int) and value >= 0:
                return value
        return 0

    def _build_run_summary_record(
        self,
        *,
        validation_run: ValidationRun,
        step_metrics: list[dict[str, Any]],
    ) -> ValidationRunSummary:
        """
        Build run and step summary records from database findings.

        This method queries persisted findings from the database rather than
        relying solely on in-memory step_metrics. This ensures accurate summaries
        in resume scenarios where earlier steps' findings are already persisted
        but not in the current step_metrics list.

        The step_metrics list is still used to extract assertion counts, which
        aren't persisted as findings (they come from validator stats).
        """
        from django.db.models import Count

        # Query run-level severity counts from persisted findings
        # This ensures we include findings from ALL steps, not just current pass
        severity_totals: Counter[str] = Counter()
        for row in (
            ValidationFinding.objects.filter(validation_run=validation_run)
            .values("severity")
            .annotate(count=Count("id"))
        ):
            severity_totals[row["severity"]] = row["count"]

        total_findings = sum(severity_totals.values())

        # Extract assertion counts from step_metrics (not persisted as findings)
        assertion_failures = sum(
            metrics.get("assertion_failures", 0) for metrics in step_metrics
        )
        assertion_total = sum(
            metrics.get("assertion_total", 0) for metrics in step_metrics
        )

        summary_record, _ = ValidationRunSummary.objects.update_or_create(
            run=validation_run,
            defaults={
                "status": validation_run.status,
                "completed_at": validation_run.ended_at,
                "total_findings": total_findings,
                "error_count": severity_totals.get(Severity.ERROR.value, 0),
                "warning_count": severity_totals.get(Severity.WARNING.value, 0),
                "info_count": severity_totals.get(Severity.INFO.value, 0),
                "assertion_failure_count": assertion_failures,
                "assertion_total_count": assertion_total,
                "extras": {},
            },
        )

        # Build step summaries from ALL step runs, querying findings from DB
        summary_record.step_summaries.all().delete()
        step_summary_objects: list[ValidationStepRunSummary] = []

        all_step_runs = ValidationStepRun.objects.filter(
            validation_run=validation_run,
        ).select_related("workflow_step").order_by("step_order")

        for step_run in all_step_runs:
            # Query step-level severity counts from persisted findings
            step_severity_counts: Counter[str] = Counter()
            for row in (
                ValidationFinding.objects.filter(validation_step_run=step_run)
                .values("severity")
                .annotate(count=Count("id"))
            ):
                step_severity_counts[row["severity"]] = row["count"]

            step_summary_objects.append(
                ValidationStepRunSummary(
                    summary=summary_record,
                    step_run=step_run,
                    step_name=getattr(
                        step_run.workflow_step,
                        "name",
                        "",
                    ),
                    step_order=step_run.step_order or 0,
                    status=step_run.status,
                    error_count=step_severity_counts.get(Severity.ERROR.value, 0),
                    warning_count=step_severity_counts.get(Severity.WARNING.value, 0),
                    info_count=step_severity_counts.get(Severity.INFO.value, 0),
                ),
            )

        if step_summary_objects:
            ValidationStepRunSummary.objects.bulk_create(step_summary_objects)

        return summary_record

    def execute_workflow_step(
        self,
        step: WorkflowStep,
        validation_run: ValidationRun,
    ) -> ValidationResult:
        """
        Dispatch a single workflow step to its handler.

        This is the central dispatcher that routes steps to the correct handler:

        1. Builds a RunContext with the validation_run, step, and any signals
           from prior steps (for cross-step CEL assertions).

        2. Resolves the handler:
           - For validator steps: uses ValidatorStepHandler
           - For action steps: looks up handler class from ACTION_HANDLER_REGISTRY

        3. Calls handler.execute(run_context) and maps the StepResult back to
           ValidationResult for backwards compatibility with callers.

        Args:
            step: The WorkflowStep to execute (has either .validator or .action).
            validation_run: The parent ValidationRun being processed.

        Returns:
            ValidationResult with passed=True/False for sync handlers, or
            passed=None for async handlers (EnergyPlus, FMI) that have launched
            a Cloud Run Job and are awaiting callback.
        """
        # 1. Prepare Context
        from validibot.actions.handlers import ValidatorStepHandler
        from validibot.actions.protocols import RunContext
        from validibot.actions.protocols import StepResult
        from validibot.actions.registry import get_action_handler

        signals = self._extract_downstream_signals(validation_run)
        context = RunContext(
            validation_run=validation_run,
            step=step,
            downstream_signals=signals,
        )

        # 2. Resolve Handler
        handler = None

        if step.validator:
            handler = ValidatorStepHandler()
        elif step.action:
            action_type = step.action.definition.type
            handler_class = get_action_handler(action_type)
            if handler_class:
                handler = handler_class()

        # 3. Handle missing handler gracefully
        if handler is None:
            if step.action:
                error_msg = f"No handler registered for action type: {action_type}"
                logger.error(
                    "No handler for action: type=%s step_id=%s run_id=%s",
                    action_type,
                    getattr(step, "id", None),
                    validation_run.id,
                )
            else:
                error_msg = _("WorkflowStep has no validator or action configured.")
                logger.error(
                    "WorkflowStep has no validator or action: step_id=%s run_id=%s",
                    getattr(step, "id", None),
                    validation_run.id,
                )
            step_result = StepResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=error_msg,
                        severity=Severity.ERROR,
                        code="missing_handler",
                    ),
                ],
            )
            return ValidationResult(
                passed=step_result.passed,
                issues=[self._normalize_issue(i) for i in step_result.issues],
                stats=step_result.stats,
            )

        # 4. Execute Handler
        step_result = handler.execute(context)

        # 5. Map Result (Backwards Compatibility)
        # We return a ValidationResult because callers (execute method) expect it.
        # Future refactor: Update callers to use StepResult directly.
        validation_result = ValidationResult(
            passed=step_result.passed,
            issues=[self._normalize_issue(i) for i in step_result.issues],
            stats=step_result.stats,
        )
        validation_result.workflow_step_name = step.name

        logger.info(
            "Step executed: step_id=%s handler=%s passed=%s",
            getattr(step, "id", None),
            handler.__class__.__name__,
            validation_result.passed,
        )

        return validation_result

    def _extract_downstream_signals(
        self,
        validation_run: ValidationRun | None,
    ) -> dict[str, Any]:
        """
        Collect signals from completed steps for cross-step CEL assertions.

        When a validator emits signals (e.g., EnergyPlus outputs like zone_temp),
        they're stored in validation_run.summary["steps"][step_id]["signals"].
        This method extracts them into a structure that CEL can query:

            steps.<step_run_id>.signals.<catalog_slug>

        Example: An EnergyPlus step with id=42 emitting {"zone_temp": 21.5}
        allows later steps to assert: steps["42"].signals.zone_temp < 25

        Returns:
            Dict mapping step_run_id to {"signals": {...}} for each prior step.
        """
        if not validation_run:
            return {}
        summary = getattr(validation_run, "summary", None) or {}
        if not isinstance(summary, dict):
            return {}
        steps = summary.get("steps", {}) or {}
        if not isinstance(steps, dict):
            return {}
        scoped_signals: dict[str, Any] = {}
        for key, value in steps.items():
            if isinstance(value, dict):
                scoped_signals[str(key)] = {
                    "signals": value.get("signals", {}) or {},
                }
        return scoped_signals

    def _resolve_run_actor(
        self,
        validation_run: ValidationRun,
        user_id: int | None,
    ):
        if getattr(validation_run, "user_id", None):
            return validation_run.user
        submission_user = getattr(
            getattr(validation_run, "submission", None),
            "user",
            None,
        )
        if submission_user and getattr(submission_user, "is_authenticated", False):
            return submission_user
        if user_id:
            from django.contrib.auth import get_user_model

            UserModel = get_user_model()  # noqa: N806

            try:
                return UserModel.objects.get(pk=user_id)
            except UserModel.DoesNotExist:
                return None
        return None
