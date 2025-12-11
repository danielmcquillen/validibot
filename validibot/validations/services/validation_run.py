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
from validibot.validations.constants import VALIDATION_RUN_TERMINAL_STATUSES
from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunSource
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.engines.base import ValidationIssue
from validibot.validations.engines.base import ValidationResult
from validibot.validations.engines.registry import get as get_validator_class
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
    from validibot.validations.engines.base import BaseValidatorEngine
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Validator
    from validibot.workflows.models import Workflow
    from validibot.workflows.models import WorkflowStep


@dataclass
class ValidationRunLaunchResults:
    validation_run: ValidationRun
    data: dict[str, Any] = field(factory=dict)
    status: int | None = None

    @property
    def status_code(self) -> int | None:  # Backwards compatibility for legacy callers
        return self.status

    @status_code.setter
    def status_code(self, value: int | None) -> None:
        self.status = value


class ValidationRunService:
    """
    Single service for 'launching' and then 'executing' validation runs.
    There are two main methods in this class:

    1. launch():    called by views to create a run and execute it.
    2. execute():   performs the validation steps.

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
        wait_for_completion: bool = False,
    ) -> ValidationRunLaunchResults:
        """
        Creates a validation run for a given workflow and a user request.
        The user should have provided us with a 'submission' as part of their request.
        The submission is the content (json, xml, whatever) that the workflow will
        validate.

        When ``wait_for_completion`` is True we optimistically execute inline.
        If the run reaches a terminal status we return 201 Created; otherwise
        the caller gets a 202 Accepted response plus a link to check status
        later. The default behaviour for UI launches is to return immediately
        so the browser can transition to the in-progress page.

        Args:
            request:                The HTTP request object.
            org:                    The organization under which the validation
                                    run is created.
            workflow:               The workflow to be executed.
            submission:             The submission associated with the validation run.
            user_id:                The ID of the user initiating the run.
            metadata:               Optional metadata to be associated with the run.
            wait_for_completion:    Whether to wait synchronously for the run to
                                    complete.
            source:                 Origin of the run (launch page, API, etc.).

        Returns:
            ValidationRunLaunchResults: Instance of this dataclass with
            results of launch.

        """
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
        if BILLING_ENFORCEMENT_ENABLED:
            self._check_billing_limits(org, workflow)

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

        # Execute immediately (Celery removed). This preserves existing state
        # transitions and pending async markers for Cloud Run validators.
        try:
            self.execute(
                validation_run_id=validation_run.id,
                user_id=request.user.id,
                metadata=metadata,
            )
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "Validation run %s failed during execution",
                validation_run.id,
            )
            validation_run.refresh_from_db()
            validation_run.status = ValidationRunStatus.FAILED
            validation_run.error = GENERIC_EXECUTION_ERROR
            validation_run.save(update_fields=["status", "error"])

        validation_run.refresh_from_db()

        results: ValidationRunLaunchResults = ValidationRunLaunchResults(
            validation_run=validation_run,
        )

        if validation_run.status in VALIDATION_RUN_TERMINAL_STATUSES:
            # Finished (either success or failure)
            results.status = status.HTTP_201_CREATED
        else:
            # Still running or pending
            # Add the URL to poll for status
            results.status = status.HTTP_202_ACCEPTED

        logger.info(
            "Validation run %s launch completed in %.2f ms (status=%s)",
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

    # ---------- Billing Enforcement ----------

    def _check_billing_limits(
        self,
        org: Organization,
        workflow: Workflow,
    ) -> None:
        """
        Check billing limits before creating a validation run.

        For basic workflows: increments the usage counter and checks monthly limit.
        For advanced workflows: checks credit balance.

        Raises:
            BillingError (or subclass) if limits exceeded or subscription inactive.
        """
        # Local imports to avoid circular dependencies
        from validibot.billing.metering import AdvancedWorkflowMeter
        from validibot.billing.metering import BasicWorkflowMeter

        # Check if org has a subscription (it should, but handle edge cases)
        if not hasattr(org, "subscription"):
            logger.warning(
                "Organization %s has no subscription, skipping billing check",
                org.id,
            )
            return

        # Determine if workflow is advanced (uses high-compute validators)
        is_advanced = getattr(workflow, "is_advanced", False)

        if is_advanced:
            # For advanced workflows, check credit balance
            # Credit deduction happens after the run completes
            AdvancedWorkflowMeter().check_can_launch(org, credits_required=1)
        else:
            # For basic workflows, check and increment usage counter
            BasicWorkflowMeter().check_and_increment(org)

    # ---------- Execute ----------

    def execute(
        self,
        validation_run_id: int,
        user_id: int,
        metadata: dict | None = None,
    ) -> ValidationRunTaskResult:
        """
        Execute a ValidationRun in-process.
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

        if validation_run.status not in (
            ValidationRunStatus.PENDING,
            ValidationRunStatus.RUNNING,
        ):
            return ValidationRunTaskResult(
                run_id=validation_run.id,
                status=validation_run.status,
                error=_("Validation run is not in a state that allows execution."),
            )

        tracking_service = TrackingEventService()
        actor = self._resolve_run_actor(validation_run, user_id)

        def _was_cancelled() -> bool:
            validation_run.refresh_from_db(fields=["status"])
            return validation_run.status == ValidationRunStatus.CANCELED

        validation_run.status = ValidationRunStatus.RUNNING
        if not validation_run.started_at:
            validation_run.started_at = timezone.now()

        validation_run.save(update_fields=["status", "started_at"])
        tracking_service.log_validation_run_started(
            run=validation_run,
            user=actor,
            extra_data={"status": ValidationRunStatus.RUNNING},
        )

        workflow: Workflow = validation_run.workflow
        overall_failed = False
        pending_async = False
        failing_step_id = None
        cancelled = False
        step_metrics: list[dict[str, Any]] = []

        try:
            workflow_steps = workflow.steps.all().order_by("order")
            for wf_step in workflow_steps:
                if _was_cancelled():
                    cancelled = True
                    break
                step_run = self._start_step_run(
                    validation_run=validation_run,
                    workflow_step=wf_step,
                )
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
            validation_run.summary = {}
            validation_run.save(
                update_fields=["status", "ended_at", "error", "summary"],
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
        else:
            validation_run.status = ValidationRunStatus.SUCCEEDED
            validation_run.error = ""
        validation_run.ended_at = timezone.now()
        validation_run.summary = {}
        validation_run.save(
            update_fields=["status", "error", "ended_at", "summary"],
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

    def _start_step_run(
        self,
        *,
        validation_run: ValidationRun,
        workflow_step: WorkflowStep,
    ) -> ValidationStepRun:
        """Create a ValidationStepRun entry marking the step as in progress."""
        return ValidationStepRun.objects.create(
            validation_run=validation_run,
            workflow_step=workflow_step,
            step_order=workflow_step.order or 0,
            status=StepStatus.RUNNING,
            started_at=timezone.now(),
        )

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
        severity_totals: Counter = Counter()
        total_findings = 0
        for metrics in step_metrics:
            severity_totals.update(metrics.get("severity_counts", {}))
            total_findings += metrics.get("total_findings", 0)

        summary_record, _ = ValidationRunSummary.objects.update_or_create(
            run=validation_run,
            defaults={
                "status": validation_run.status,
                "completed_at": validation_run.ended_at,
                "total_findings": total_findings,
                "error_count": severity_totals.get(Severity.ERROR, 0),
                "warning_count": severity_totals.get(Severity.WARNING, 0),
                "info_count": severity_totals.get(Severity.INFO, 0),
                "assertion_failure_count": sum(
                    metrics.get("assertion_failures", 0) for metrics in step_metrics
                ),
                "assertion_total_count": sum(
                    metrics.get("assertion_total", 0) for metrics in step_metrics
                ),
                "extras": {},
            },
        )

        summary_record.step_summaries.all().delete()
        step_summary_objects: list[ValidationStepRunSummary] = []
        for metrics in step_metrics:
            step_run = metrics.get("step_run")
            if not step_run:
                continue
            severity_counts: Counter = metrics.get("severity_counts", Counter())
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
                    error_count=severity_counts.get(Severity.ERROR, 0),
                    warning_count=severity_counts.get(Severity.WARNING, 0),
                    info_count=severity_counts.get(Severity.INFO, 0),
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
        if submission and not validator.supports_file_type(submission.file_type):
            issue = ValidationIssue(
                path="",
                message=_(
                    "Submission file type '%(ft)s' is not supported by this validator."
                )
                % {"ft": submission.file_type},
                severity=Severity.ERROR,
                code="unsupported_file_type",
            )
            return ValidationResult(
                passed=False,
                issues=[issue],
                stats={"file_type": submission.file_type},
            )

        # 3) Run the validator (registry resolves the concrete class by type/variant)
        step_config = getattr(step, "config", {}) or {}
        validation_result: ValidationResult = self.run_validator_engine(
            validator=validator,
            submission=submission,
            ruleset=ruleset,
            config=step_config,
            validation_run=validation_run,
            step=step,
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
        *,
        validation_run: ValidationRun | None = None,
        step: WorkflowStep | None = None,
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

        signals = self._extract_downstream_signals(validation_run)
        if getattr(validator_engine, "run_context", None) is None:
            from types import SimpleNamespace

            validator_engine.run_context = SimpleNamespace(
                validation_run=validation_run,
                workflow_step=step,
                downstream_signals=signals,
            )
        else:
            validator_engine.run_context.validation_run = validation_run
            validator_engine.run_context.workflow_step = step
            validator_engine.run_context.downstream_signals = signals

        if hasattr(validator_engine, "validate_with_run"):
            validation_result = validator_engine.validate_with_run(
                validator=validator,
                submission=submission,
                ruleset=ruleset,
                run=validation_run,
                step=step,
            )
        else:
            validation_result = validator_engine.validate(
                validator=validator,
                submission=submission,
                ruleset=ruleset,
            )

        return validation_result

    def _extract_downstream_signals(
        self,
        validation_run: ValidationRun | None,
    ) -> dict[str, Any]:
        """
        Collect namespaced signals from prior steps so engines can expose them
        to CEL assertions (steps.<step_run_id>.signals.<slug>).
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
