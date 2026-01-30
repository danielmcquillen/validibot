"""
Base class for validation step processors.

Processors handle lifecycle orchestration while engines handle validation
logic and assertion evaluation.
"""

from __future__ import annotations

import logging
from abc import ABC
from abc import abstractmethod
from collections import Counter
from typing import TYPE_CHECKING
from typing import Any

from django.utils import timezone

from validibot.actions.protocols import RunContext
from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.engines.base import AssertionStats
from validibot.validations.engines.base import ValidationIssue
from validibot.validations.models import ValidationFinding

if TYPE_CHECKING:
    from validibot.validations.models import ValidationRun
    from validibot.validations.models import ValidationStepRun
    from validibot.validations.services.step_processor.result import (
        StepProcessingResult,
    )

logger = logging.getLogger(__name__)


class ValidationStepProcessor(ABC):
    """
    Base class for processing a single validation step.

    Processors handle LIFECYCLE ONLY:
    - Call engine methods at the right time
    - Persist findings from engine results
    - Handle errors and set appropriate status
    - Finalize step with timing

    Processors do NOT evaluate assertions or extract signals - that's the
    engine's job. Processors only persist what the engine returns.
    """

    def __init__(
        self,
        validation_run: ValidationRun,
        step_run: ValidationStepRun,
    ):
        self.validation_run = validation_run
        self.step_run = step_run
        self.workflow_step = step_run.workflow_step
        self.validator = self.workflow_step.validator
        self.ruleset = self.workflow_step.ruleset

    @abstractmethod
    def execute(self) -> StepProcessingResult:
        """Execute the validation step. Subclasses implement this."""
        ...

    # ──────────────────────────────────────────────────────────────
    # Shared methods used by both subclasses
    # ──────────────────────────────────────────────────────────────

    def _get_engine(self):
        """Get the validator engine instance from the registry."""
        from validibot.validations.engines.registry import get as get_engine_class

        engine_cls = get_engine_class(self.validator.validation_type)
        return engine_cls()

    def _build_run_context(self) -> RunContext:
        """Build RunContext for engine calls."""
        return RunContext(
            validation_run=self.validation_run,
            step=self.workflow_step,
            downstream_signals=self._get_downstream_signals(),
        )

    def _get_downstream_signals(self) -> dict[str, Any]:
        """Extract signals from prior steps for cross-step assertions."""
        summary = self.validation_run.summary or {}
        return summary.get("steps", {})

    def persist_findings(
        self,
        issues: list[ValidationIssue],
        *,
        append: bool = False,
    ) -> tuple[Counter, int]:
        """
        Persist ValidationFinding records from issues.

        Args:
            issues: List of ValidationIssue objects from engine
            append: If True, add to existing findings. If False, replace.
                    Default False for simple validators, True for async callbacks.

        Returns:
            Tuple of (severity_counts, assertion_failures)
        """
        if not append:
            # Delete existing findings for this step
            ValidationFinding.objects.filter(
                validation_step_run=self.step_run,
            ).delete()

        severity_counts: Counter = Counter()
        assertion_failures = 0

        findings_to_create = []
        for issue in issues:
            severity = self._coerce_severity(issue.severity)
            severity_counts[severity] += 1

            # Count assertion failures: any assertion issue that is NOT a success.
            # SUCCESS-severity issues indicate the assertion PASSED (expression=true).
            # All other severities (ERROR, WARNING, INFO) indicate the assertion
            # FAILED (expression=false), just with different importance levels.
            if issue.assertion_id is not None and severity != Severity.SUCCESS.value:
                assertion_failures += 1

            finding = ValidationFinding(
                validation_run=self.validation_run,
                validation_step_run=self.step_run,
                path=issue.path or "",
                message=issue.message,
                severity=severity,
                code=issue.code or "",
                meta=issue.meta or {},
                ruleset_assertion_id=issue.assertion_id,
            )
            # Call model methods to ensure consistency
            finding._ensure_run_alignment()  # noqa: SLF001
            finding._strip_payload_prefix()  # noqa: SLF001
            findings_to_create.append(finding)

        if findings_to_create:
            ValidationFinding.objects.bulk_create(findings_to_create, batch_size=500)

        return severity_counts, assertion_failures

    def _coerce_severity(self, severity: Any) -> str:
        """Coerce severity to a valid Severity value."""
        if isinstance(severity, str):
            # Handle both Severity enum and string values
            severity_upper = severity.upper()
            if severity_upper in {s.value for s in Severity}:
                return severity_upper
            # Try to match by name
            for s in Severity:
                if s.name == severity_upper:
                    return s.value
        if hasattr(severity, "value"):
            return severity.value
        return Severity.ERROR.value

    def store_assertion_counts(
        self,
        assertion_failures: int,
        assertion_total: int,
    ) -> None:
        """
        Store assertion counts in step_run.output for run summary.

        These fields are used by _build_run_summary_record() to calculate
        overall assertion pass/fail counts.
        """
        output = self.step_run.output or {}
        output["assertion_failures"] = assertion_failures
        output["assertion_total"] = assertion_total
        self.step_run.output = output
        self.step_run.save(update_fields=["output"])

    def _get_stored_assertion_stats(self) -> AssertionStats:
        """Get assertion stats stored from input-stage (for callback path)."""
        output = self.step_run.output or {}
        return AssertionStats(
            total=output.get("assertion_total", 0),
            failures=output.get("assertion_failures", 0),
        )

    def store_signals(self, signals: dict[str, Any]) -> None:
        """
        Store signals in run.summary for downstream steps.

        Signals are already extracted by the engine (during assertion evaluation)
        and passed here via ValidationResult.signals. The processor just persists
        them.

        Signals are stored at: run.summary["steps"][step_run_id]["signals"]
        """
        if not signals:
            return

        summary = self.validation_run.summary or {}
        steps = summary.setdefault("steps", {})
        step_key = str(self.step_run.id)
        step_data = steps.setdefault(step_key, {})
        step_data["signals"] = signals

        self.validation_run.summary = summary
        self.validation_run.save(update_fields=["summary"])

    def finalize_step(
        self,
        status: StepStatus,
        stats: dict[str, Any],
        error: str | None = None,
    ) -> None:
        """
        Mark step complete with timing and output.

        Sets ended_at, duration_ms, status, output, and error fields.
        """
        now = timezone.now()
        self.step_run.ended_at = now

        if self.step_run.started_at:
            duration = now - self.step_run.started_at
            self.step_run.duration_ms = int(duration.total_seconds() * 1000)

        self.step_run.status = status.value if hasattr(status, "value") else status

        # Merge stats into existing output (preserve assertion counts)
        output = self.step_run.output or {}
        output.update(stats)
        self.step_run.output = output

        if error:
            self.step_run.error = error

        self.step_run.save(
            update_fields=["ended_at", "duration_ms", "status", "output", "error"],
        )
