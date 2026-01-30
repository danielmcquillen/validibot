"""
EnergyPlus validation engine.

This engine forwards incoming EnergyPlus submissions (epJSON or IDF) to
container-based validators via the ExecutionBackend abstraction. This works
across different deployment targets:

- Self-hosted: Docker containers via local socket (synchronous)
- GCP: Cloud Run Jobs (async with callbacks)
- AWS: AWS Batch (future)

## Validation Flow

1. Engine receives validator, submission, ruleset from workflow execution
2. run_context is set by the handler with validation_run and workflow_step
3. Gets the configured ExecutionBackend
4. Builds ExecutionRequest and calls backend.execute()
5. For sync backends: Returns ValidationResult immediately
6. For async backends: Returns pending result, callback delivers results later

## Output Envelope Structure

The EnergyPlus validator container produces an `EnergyPlusOutputEnvelope`
(from vb_shared.energyplus.envelopes) containing:

- outputs.metrics: EnergyPlusSimulationMetrics with fields like:
  - site_eui_kwh_m2: Site energy use intensity
  - site_electricity_kwh: Total electricity consumption
  - site_natural_gas_kwh: Total gas consumption
  - etc. (see vb_shared/energyplus/models.py)

These metrics are extracted via `extract_output_signals()` for use in
output-stage CEL assertions (e.g., "site_eui_kwh_m2 < 100").
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _

from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.engines.base import AssertionStats
from validibot.validations.engines.base import BaseValidatorEngine
from validibot.validations.engines.base import ValidationIssue
from validibot.validations.engines.base import ValidationResult
from validibot.validations.engines.registry import register_engine

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.submissions.models import Submission
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Validator


@register_engine(ValidationType.ENERGYPLUS)
class EnergyPlusValidationEngine(BaseValidatorEngine):
    """
    Run submitted epJSON through container-based validators.

    This engine dispatches validation to the configured ExecutionBackend:
    - Self-hosted: Docker containers (synchronous)
    - GCP: Cloud Run Jobs (async with callbacks)
    - AWS: AWS Batch (future)

    The ValidationRun is updated via callback when async jobs complete.
    """

    @classmethod
    def extract_output_signals(cls, output_envelope: Any) -> dict[str, Any] | None:
        """
        Extract simulation metrics from an EnergyPlus output envelope.

        EnergyPlus envelopes (EnergyPlusOutputEnvelope from vb_shared) store
        metrics in outputs.metrics as an EnergyPlusSimulationMetrics Pydantic
        model. Fields include site_eui_kwh_m2, site_electricity_kwh, etc.

        Args:
            output_envelope: EnergyPlusOutputEnvelope from the validator container.

        Returns:
            Dict of metrics keyed by field name (matching catalog slugs), with
            None values filtered out. Returns None if extraction fails.
        """
        try:
            outputs = getattr(output_envelope, "outputs", None)
            if not outputs:
                return None

            metrics = getattr(outputs, "metrics", None)
            if not metrics:
                return None

            # Pydantic model_dump converts to dict; filter None values
            if hasattr(metrics, "model_dump"):
                metrics_dict = metrics.model_dump(mode="json")
                return {k: v for k, v in metrics_dict.items() if v is not None}

            # Fallback if metrics is already a dict
            if isinstance(metrics, dict):
                return {k: v for k, v in metrics.items() if v is not None}
        except Exception:
            logger.debug("Could not extract assertion signals from EnergyPlus envelope")

        return None

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset | None,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """
        Validate an EnergyPlus submission.

        Dispatches to the configured ExecutionBackend and returns results.
        For sync backends (Docker), returns immediately. For async backends
        (Cloud Run, AWS Batch), returns pending and callback delivers results.

        Args:
            validator: EnergyPlus validator instance
            submission: Submission with IDF/epJSON content
            ruleset: Ruleset with weather_file metadata
            run_context: Required execution context with validation_run and step

        Returns:
            ValidationResult with passed=True/False for sync backends,
            passed=None (pending) for async backends, or passed=False (error)
            if not configured or missing context.
        """
        # Store run_context on instance for CEL evaluation methods
        self.run_context = run_context

        # Validate that run_context is properly set
        run = run_context.validation_run if run_context else None
        step = run_context.step if run_context else None

        if not run or not step:
            logger.error(
                "EnergyPlus engine requires run_context to be set with "
                "validation_run and workflow_step"
            )
            issues = [
                ValidationIssue(
                    path="",
                    message=_(
                        "EnergyPlus validation requires workflow context. "
                        "Ensure the engine is called via the workflow handler.",
                    ),
                    severity=Severity.ERROR,
                ),
            ]
            return ValidationResult(
                passed=False,
                issues=issues,
                stats={"implementation_status": "Missing run_context"},
            )

        # Use the unified execution backend system
        # This handles both self-hosted (Docker) and cloud (GCP, AWS) execution
        from validibot.validations.services.execution import get_execution_backend
        from validibot.validations.services.execution.base import ExecutionRequest

        backend = get_execution_backend()

        if not backend.is_available():
            logger.warning(
                "Execution backend '%s' is not available",
                backend.backend_name,
            )
            issues = [
                ValidationIssue(
                    path="",
                    message=_(
                        "Validation backend is not available. "
                        "Check your deployment configuration."
                    ),
                    severity=Severity.ERROR,
                ),
            ]
            return ValidationResult(
                passed=False,
                issues=issues,
                stats={"implementation_status": "Backend not available"},
            )

        # Build execution request
        request = ExecutionRequest(
            run=run,
            validator=validator,
            submission=submission,
            step=step,
        )

        # Execute using the backend
        response = backend.execute(request)

        # Convert ExecutionResponse to ValidationResult
        return self._response_to_result(response, is_async=backend.is_async)

    def _response_to_result(
        self,
        response,  # ExecutionResponse
        *,
        is_async: bool,
    ) -> ValidationResult:
        """
        Convert an ExecutionResponse to a ValidationResult.

        For async backends, returns a pending result (passed=None).
        For sync backends, extracts issues from the output envelope.

        Args:
            response: ExecutionResponse from the backend
            is_async: Whether the backend is async

        Returns:
            ValidationResult with appropriate pass/fail/pending status
        """
        # Build stats from response metadata
        stats = {
            "execution_id": response.execution_id,
            "input_uri": response.input_uri,
            "output_uri": response.output_uri,
            "execution_bundle_uri": response.execution_bundle_uri,
            "is_async": is_async,
        }
        if response.duration_seconds is not None:
            stats["duration_seconds"] = response.duration_seconds

        # Async execution - return pending result
        if is_async and not response.is_complete:
            return ValidationResult(passed=None, issues=[], stats=stats)

        # Error during execution
        if response.error_message:
            issues = [
                ValidationIssue(
                    path="",
                    message=response.error_message,
                    severity=Severity.ERROR,
                ),
            ]
            return ValidationResult(passed=False, issues=issues, stats=stats)

        # Extract results from output envelope
        if response.output_envelope is None:
            issues = [
                ValidationIssue(
                    path="",
                    message="Validation completed but no output envelope received",
                    severity=Severity.ERROR,
                ),
            ]
            return ValidationResult(passed=False, issues=issues, stats=stats)

        # For sync backends, include the output_envelope so the processor can call
        # post_execute_validate() to evaluate output-stage assertions
        result = self._process_output_envelope(response.output_envelope, stats)
        # Attach envelope for processor to use
        result.output_envelope = response.output_envelope
        return result

    def _process_output_envelope(
        self,
        envelope,  # ValidationOutputEnvelope
        stats: dict,
    ) -> ValidationResult:
        """
        Process a ValidationOutputEnvelope and extract results.

        Args:
            envelope: The output envelope from the validator container
            stats: Stats dict to include in the result

        Returns:
            ValidationResult with pass/fail based on envelope contents
        """
        from vb_shared.validations.envelopes import Severity as EnvelopeSeverity
        from vb_shared.validations.envelopes import ValidationStatus

        issues: list[ValidationIssue] = []

        # Extract messages from envelope
        for msg in envelope.messages:
            severity_map = {
                EnvelopeSeverity.ERROR: Severity.ERROR,
                EnvelopeSeverity.WARNING: Severity.WARNING,
                EnvelopeSeverity.INFO: Severity.INFO,
            }
            issues.append(
                ValidationIssue(
                    path=msg.location.path if msg.location else "",
                    message=msg.text,
                    severity=severity_map.get(msg.severity, Severity.INFO),
                )
            )

        # Include outputs in stats if available
        if envelope.outputs:
            if hasattr(envelope.outputs, "model_dump"):
                stats["outputs"] = envelope.outputs.model_dump(mode="json")
            elif isinstance(envelope.outputs, dict):
                stats["outputs"] = envelope.outputs

        # Determine pass/fail based on status
        if envelope.status == ValidationStatus.SUCCESS:
            passed = True
        elif envelope.status in (
            ValidationStatus.FAILED_VALIDATION,
            ValidationStatus.FAILED_RUNTIME,
        ):
            passed = False
        else:
            # Cancelled or unknown
            passed = False

        return ValidationResult(passed=passed, issues=issues, stats=stats)

    def post_execute_validate(
        self,
        output_envelope: Any,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """
        Process container output and evaluate output-stage assertions.

        Called after container execution completes (either sync or via callback).
        This method:
        1. Extracts signals from the envelope via extract_output_signals()
        2. Evaluates output-stage CEL assertions using those signals
        3. Extracts issues from envelope messages
        4. Returns ValidationResult with signals field populated

        Args:
            output_envelope: EnergyPlusOutputEnvelope from the validator container
            run_context: Execution context for CEL evaluation

        Returns:
            ValidationResult with output-stage issues, assertion_stats,
            and signals populated.
        """
        from vb_shared.validations.envelopes import Severity as EnvelopeSeverity
        from vb_shared.validations.envelopes import ValidationStatus

        # Store run_context for CEL evaluation methods
        self.run_context = run_context

        issues: list[ValidationIssue] = []

        # Extract messages from envelope
        for msg in output_envelope.messages:
            severity_map = {
                EnvelopeSeverity.ERROR: Severity.ERROR,
                EnvelopeSeverity.WARNING: Severity.WARNING,
                EnvelopeSeverity.INFO: Severity.INFO,
            }
            issues.append(
                ValidationIssue(
                    path=msg.location.path if msg.location else "",
                    message=msg.text,
                    severity=severity_map.get(msg.severity, Severity.INFO),
                )
            )

        # Extract signals from envelope for downstream steps and assertion evaluation
        signals = self.extract_output_signals(output_envelope) or {}

        # Evaluate output-stage CEL assertions if we have context
        assertion_issues: list[ValidationIssue] = []
        total_assertions = 0
        if run_context and run_context.step:
            # Get the validator and ruleset from the step
            validator = run_context.step.validator
            ruleset = run_context.step.ruleset

            if validator and ruleset:
                # Evaluate output-stage assertions using the extracted signals
                assertion_issues = self.evaluate_cel_assertions(
                    ruleset=ruleset,
                    validator=validator,
                    payload=signals,
                    target_stage="output",
                )
                issues.extend(assertion_issues)

                # Count output-stage assertions only
                total_assertions = self._count_stage_assertions(ruleset, "output")

        # Count assertion failures (non-SUCCESS issues from assertions).
        # SUCCESS-severity issues indicate passed assertions, not failures.
        assertion_failures = sum(
            1 for issue in assertion_issues
            if issue.severity != Severity.SUCCESS
        )

        # Determine pass/fail based on envelope status
        if output_envelope.status == ValidationStatus.SUCCESS:
            passed = True
        elif output_envelope.status in (
            ValidationStatus.FAILED_VALIDATION,
            ValidationStatus.FAILED_RUNTIME,
        ):
            passed = False
        else:
            # Cancelled or unknown
            passed = False

        # Build stats with outputs if available
        stats: dict[str, Any] = {}
        if output_envelope.outputs:
            if hasattr(output_envelope.outputs, "model_dump"):
                stats["outputs"] = output_envelope.outputs.model_dump(mode="json")
            elif isinstance(output_envelope.outputs, dict):
                stats["outputs"] = output_envelope.outputs

        return ValidationResult(
            passed=passed,
            issues=issues,
            assertion_stats=AssertionStats(
                total=total_assertions,
                failures=assertion_failures,
            ),
            signals=signals,
            stats=stats,
        )
