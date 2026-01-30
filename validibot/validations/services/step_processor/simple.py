"""
Simple validation processor for inline validators.

Handles JSON Schema, XML Schema, Basic, and AI validators that run
synchronously in the Django process.
"""

from __future__ import annotations

import logging

from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.engines.base import ValidationIssue
from validibot.validations.services.step_processor.base import ValidationStepProcessor
from validibot.validations.services.step_processor.result import StepProcessingResult

logger = logging.getLogger(__name__)


class SimpleValidationProcessor(ValidationStepProcessor):
    """
    Processor for simple validators (JSON Schema, XML Schema, Basic, AI).

    These validators:
    - Run inline in the Django process
    - Complete synchronously
    - Have a single assertion stage (input)

    The engine handles ALL validation logic including input-stage assertions.
    The processor just calls engine.validate() and persists results.
    """

    def execute(self) -> StepProcessingResult:
        """
        Execute the validation step.

        Calls the engine's validate() method (which handles validation logic
        and input-stage assertion evaluation), then persists the results.
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
            # Call engine.validate() - this does EVERYTHING:
            # - Validation logic (schema checking, AI prompting, etc.)
            # - Input-stage CEL assertion evaluation
            # - Returns combined issues with assertion outcomes
            result = engine.validate(
                validator=self.validator,
                submission=self.validation_run.submission,
                ruleset=self.ruleset,
                run_context=run_context,
            )
        except Exception as e:
            logger.exception(
                "Error executing simple validation step %s",
                self.step_run.id,
            )
            return self._handle_error(e)

        # Persist findings from engine result
        severity_counts, assertion_failures = self.persist_findings(result.issues)

        # Store assertion counts for run summary (using typed fields)
        self.store_assertion_counts(
            result.assertion_stats.failures,
            result.assertion_stats.total,
        )

        # Finalize the step
        status = StepStatus.PASSED if result.passed else StepStatus.FAILED
        self.finalize_step(status, result.stats or {})

        return StepProcessingResult(
            passed=result.passed,
            step_run=self.step_run,
            severity_counts=severity_counts,
            total_findings=sum(severity_counts.values()),
            assertion_failures=result.assertion_stats.failures,
            assertion_total=result.assertion_stats.total,
        )

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
