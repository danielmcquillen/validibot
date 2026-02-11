"""
Advanced validation processor for container-based validators.

Handles EnergyPlus and FMI validators that run in Docker containers
and may complete synchronously or asynchronously.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.engines.base import ValidationIssue
from validibot.validations.engines.base import ValidationResult
from validibot.validations.models import ValidationFinding
from validibot.validations.services.step_processor.base import ValidationStepProcessor
from validibot.validations.services.step_processor.result import StepProcessingResult

logger = logging.getLogger(__name__)


class AdvancedValidationProcessor(ValidationStepProcessor):
    """
    Processor for advanced validators (EnergyPlus, FMI).

    These validators:
    - Run in Docker containers
    - May complete synchronously (Docker Compose) or asynchronously (GCP)
    - Have two assertion stages (input and output)

    ## Execution Modes

    **Synchronous (Docker Compose, local dev, test):**
    - Container runs and blocks until complete
    - `execute()` calls `engine.post_execute_validate()` directly
    - Returns complete result immediately

    **Asynchronous (GCP Cloud Run, future AWS):**
    - Container job is launched and returns immediately
    - `execute()` returns `passed=None` (pending)
    - Later, callback arrives and `complete_from_callback()` is called

    ## Assertion Stages

    **Input stage:** Evaluated in `engine.validate()` BEFORE container launch.
    **Output stage:** Evaluated in `engine.post_execute_validate()` AFTER container
    completes.

    ## Status Semantics

    - `ValidationStatus.SUCCESS` from the container is treated as a pass even if
      the container reported ERROR messages. We surface a warning in the step
      output and logs when that happens.
    - Output-stage assertion failures always fail the step, regardless of
      container status.
    """

    def execute(self) -> StepProcessingResult:
        """
        Execute the validation step.

        Calls the engine's validate() method to launch the container.
        For sync backends, also calls post_execute_validate() to evaluate
        output-stage assertions. For async backends, returns pending.
        """
        try:
            engine = self._get_engine()
        except KeyError as e:
            logger.exception(
                "Failed to load engine for validation step %s",
                self.step_run.id,
            )
            return self._handle_engine_not_found(e)

        run_context = self._build_run_context()

        try:
            # Call engine.validate() - this:
            # - Evaluates input-stage assertions
            # - Launches the container
            # - For sync backends: blocks and returns with output_envelope
            # - For async backends: returns immediately with passed=None
            result = engine.validate(
                validator=self.validator,
                submission=self.validation_run.submission,
                ruleset=self.ruleset,
                run_context=run_context,
            )
        except Exception as e:
            logger.exception(
                "Error executing advanced validation step %s",
                self.step_run.id,
            )
            return self._handle_error(e)

        # Persist input-stage findings (from assertions evaluated in validate())
        severity_counts, assertion_failures = self.persist_findings(result.issues)

        # Handle sync vs async completion
        if result.passed is None:
            # Async execution - container launched, waiting for callback
            self._record_pending_state(result)
            return StepProcessingResult(
                passed=None,
                step_run=self.step_run,
                severity_counts=severity_counts,
                total_findings=sum(severity_counts.values()),
                assertion_failures=result.assertion_stats.failures,
                assertion_total=result.assertion_stats.total,
            )
        # Sync execution - container completed, call post_execute_validate()
        # We already persisted input-stage findings above, so we must APPEND
        # output-stage findings to preserve them.
        if result.output_envelope is None:
            return self._handle_missing_envelope()
        return self._complete_with_envelope(
            engine,
            run_context,
            result.output_envelope,
            severity_counts,
            append_findings=True,  # Preserve input-stage findings
        )

    def complete_from_callback(self, output_envelope: Any) -> StepProcessingResult:
        """
        Complete the step after receiving async callback.

        Called by ValidationCallbackService after downloading the output
        envelope from cloud storage.

        IMPORTANT: This APPENDS findings to existing ones (from input-stage
        assertions). It does NOT delete pre-existing findings.
        """
        engine = self._get_engine()
        run_context = self._build_run_context()

        # Get existing severity counts from input-stage findings
        existing_counts = self._get_existing_finding_counts()

        return self._complete_with_envelope(
            engine,
            run_context,
            output_envelope,
            existing_counts,
            append_findings=True,
        )

    def _complete_with_envelope(
        self,
        engine,
        run_context,
        output_envelope: Any,
        existing_severity_counts: Counter,
        *,
        append_findings: bool = False,
    ) -> StepProcessingResult:
        """
        Complete the step using the output envelope.

        Called by both sync execution and async callback paths.
        """
        # Call engine.post_execute_validate() - this:
        # 1. Extracts signals from envelope (for assertion evaluation)
        # 2. Evaluates output-stage assertions using those signals
        # 3. Extracts issues from envelope messages
        # 4. Returns ValidationResult with signals field populated
        post_result = engine.post_execute_validate(output_envelope, run_context)

        from validibot_shared.validations.envelopes import ValidationStatus

        container_error_issues = [
            issue
            for issue in post_result.issues
            if issue.severity == Severity.ERROR and issue.assertion_id is None
        ]
        if (
            output_envelope.status == ValidationStatus.SUCCESS
            and container_error_issues
        ):
            warning_msg = (
                "Note: the advanced validation indicated it passed, "
                "but there were errors reported."
            )
            logger.warning(
                "Advanced validator reported SUCCESS with ERROR findings: "
                "step_run_id=%s error_count=%s",
                self.step_run.id,
                len(container_error_issues),
            )
            post_result.issues.append(
                ValidationIssue(
                    path="",
                    message=warning_msg,
                    severity=Severity.WARNING,
                    code="advanced_validation_success_with_errors",
                )
            )

        # Persist output-stage findings (APPEND for callbacks)
        output_counts, output_assertion_failures = self.persist_findings(
            post_result.issues,
            append=append_findings,
        )

        # Merge severity counts
        severity_counts = existing_severity_counts + output_counts

        # Store signals for downstream steps (using typed field)
        self.store_signals(post_result.signals or {})

        # Calculate total assertion counts (input + output stages)
        input_stats = self._get_stored_assertion_stats()
        assertion_total = input_stats.total + post_result.assertion_stats.total
        assertion_failures = input_stats.failures + post_result.assertion_stats.failures

        # Store final assertion counts
        self.store_assertion_counts(assertion_failures, assertion_total)

        # Determine final status
        status = self._map_envelope_status(output_envelope.status)
        has_assertion_errors = post_result.assertion_stats.failures > 0
        if has_assertion_errors:
            status = StepStatus.FAILED
        error = self._extract_error(output_envelope)

        # Include full envelope in step output (JSON-safe serialization)
        stats = self._serialize_envelope(output_envelope)
        if isinstance(stats, dict):
            stats["signals"] = post_result.signals or {}
        if (
            output_envelope.status == ValidationStatus.SUCCESS
            and container_error_issues
        ):
            warnings = stats.get("warnings", []) if isinstance(stats, dict) else []
            warnings.append(
                "Note: the advanced validation indicated it passed, "
                "but there were errors reported."
            )
            stats["warnings"] = warnings
        self.finalize_step(status, stats, error)

        return StepProcessingResult(
            passed=(status == StepStatus.PASSED),
            step_run=self.step_run,
            severity_counts=severity_counts,
            total_findings=sum(severity_counts.values()),
            assertion_failures=assertion_failures,
            assertion_total=assertion_total,
        )

    def _record_pending_state(self, result: ValidationResult) -> None:
        """Record execution metadata and input-stage assertion stats for async steps."""
        self.step_run.output = {
            "assertion_total": result.assertion_stats.total,
            "assertion_failures": result.assertion_stats.failures,
            **(result.stats or {}),
        }
        self.step_run.status = StepStatus.RUNNING
        self.step_run.save(update_fields=["output", "status"])

    def _get_existing_finding_counts(self) -> Counter:
        """Get severity counts from existing findings (for callback path)."""
        counts: Counter = Counter()
        findings = ValidationFinding.objects.filter(
            validation_step_run=self.step_run,
        )
        for finding in findings:
            counts[finding.severity] += 1
        return counts

    def _map_envelope_status(self, envelope_status) -> StepStatus:
        """Map ValidationStatus from envelope to StepStatus."""
        from validibot_shared.validations.envelopes import ValidationStatus

        mapping = {
            ValidationStatus.SUCCESS: StepStatus.PASSED,
            ValidationStatus.FAILED_VALIDATION: StepStatus.FAILED,
            ValidationStatus.FAILED_RUNTIME: StepStatus.FAILED,
            ValidationStatus.CANCELLED: StepStatus.SKIPPED,
        }
        return mapping.get(envelope_status, StepStatus.FAILED)

    def _extract_error(self, output_envelope) -> str:
        """Extract error message from envelope."""
        from validibot_shared.validations.envelopes import ValidationStatus

        if output_envelope.status == ValidationStatus.SUCCESS:
            return ""

        error_messages = [
            msg.text
            for msg in output_envelope.messages
            if str(msg.severity).upper() == "ERROR"
        ]
        return "\n".join(error_messages)

    def _serialize_envelope(self, output_envelope) -> dict:
        """Serialize envelope to JSON-safe dict for step_run.output."""
        if hasattr(output_envelope, "model_dump"):
            return output_envelope.model_dump(mode="json")
        if isinstance(output_envelope, dict):
            return output_envelope
        return {}

    def _handle_error(self, error: Exception) -> StepProcessingResult:
        """Handle validation errors gracefully."""
        issues = [
            ValidationIssue(
                path="",
                message=str(error),
                severity=Severity.ERROR,
            ),
        ]
        severity_counts, _ = self.persist_findings(issues)
        self.finalize_step(StepStatus.FAILED, {}, error=str(error))

        return StepProcessingResult(
            passed=False,
            step_run=self.step_run,
            severity_counts=severity_counts,
            total_findings=1,
            assertion_failures=0,
            assertion_total=0,
        )

    def _handle_engine_not_found(self, error: Exception) -> StepProcessingResult:
        """Handle missing/unregistered engine gracefully."""
        error_msg = (
            f"Failed to load validator engine for type "
            f"'{self.validator.validation_type}': {error}"
        )
        issues = [
            ValidationIssue(
                path="",
                message=error_msg,
                severity=Severity.ERROR,
                code="engine_not_found",
            ),
        ]
        severity_counts, _ = self.persist_findings(issues)
        self.finalize_step(StepStatus.FAILED, {}, error=error_msg)

        return StepProcessingResult(
            passed=False,
            step_run=self.step_run,
            severity_counts=severity_counts,
            total_findings=1,
            assertion_failures=0,
            assertion_total=0,
        )

    def _handle_missing_envelope(self) -> StepProcessingResult:
        """Handle case where sync backend didn't return envelope."""
        issues = [
            ValidationIssue(
                path="",
                message="Sync execution completed but no output envelope received. "
                "This indicates a backend configuration issue.",
                severity=Severity.ERROR,
            ),
        ]
        severity_counts, _ = self.persist_findings(issues)
        self.finalize_step(StepStatus.FAILED, {}, error="Missing output envelope")

        return StepProcessingResult(
            passed=False,
            step_run=self.step_run,
            severity_counts=severity_counts,
            total_findings=1,
            assertion_failures=0,
            assertion_total=0,
        )
