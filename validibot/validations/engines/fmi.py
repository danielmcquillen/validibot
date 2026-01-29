"""
FMI validation engine.

This engine forwards FMU submissions to container-based validators via the
ExecutionBackend abstraction. The FMU is executed in a containerized environment
with the FMI runtime.

This works across different deployment targets:
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

The FMI validator container produces an `FMIOutputEnvelope` (from
vb_shared.fmi.envelopes) containing:

- outputs.output_values: Dict keyed by catalog slug with simulation outputs
  - Each key is a catalog entry slug (e.g., "indoor_temp_c")
  - Values are the simulation outputs for that signal

These output values are extracted via `extract_output_signals()` for use in
output-stage CEL assertions (e.g., "indoor_temp_c < 26").
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _

from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
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


@register_engine(ValidationType.FMI)
class FMIValidationEngine(BaseValidatorEngine):
    """
    Run FMI validators through Cloud Run Jobs.

    This engine uploads the FMU and input bindings to GCS, triggers a Cloud Run
    Job that executes the FMU simulation, and receives results via callback.
    """

    @classmethod
    def extract_output_signals(cls, output_envelope: Any) -> dict[str, Any] | None:
        """
        Extract output values from an FMI output envelope.

        FMI envelopes (FMIOutputEnvelope from vb_shared) store simulation outputs
        in outputs.output_values as a dict keyed by catalog slug.

        Args:
            output_envelope: FMIOutputEnvelope from the Cloud Run Job.

        Returns:
            Dict of output values keyed by catalog slug. Returns None if
            extraction fails.
        """
        try:
            outputs = getattr(output_envelope, "outputs", None)
            if not outputs:
                return None

            output_values = getattr(outputs, "output_values", None)
            if not output_values:
                return None

            # Handle Pydantic model
            if hasattr(output_values, "model_dump"):
                return output_values.model_dump(mode="json")

            # Handle plain dict
            if isinstance(output_values, dict):
                return output_values
        except Exception:
            logger.debug("Could not extract assertion signals from FMI envelope")

        return None

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset | None,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """
        Validate an FMI submission.

        Launches a Cloud Run Job asynchronously and returns a pending result.

        Args:
            validator: FMI validator instance with FMU model attached
            submission: Submission with input values
            ruleset: Optional ruleset (not typically used for FMI)
            run_context: Required execution context with validation_run and step

        Returns:
            ValidationResult with passed=None (pending) if Cloud Run Jobs configured,
            or passed=False (error) if not configured or missing context.
        """
        # Store run_context on instance for CEL evaluation methods
        self.run_context = run_context

        # Validate that run_context is properly set
        run = run_context.validation_run if run_context else None
        step = run_context.step if run_context else None

        if not run or not step:
            logger.error(
                "FMI engine requires run_context to be set with "
                "validation_run and workflow_step"
            )
            issues = [
                ValidationIssue(
                    path="",
                    message=_(
                        "FMI validation requires workflow context. "
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

        return self._process_output_envelope(response.output_envelope, stats)

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
