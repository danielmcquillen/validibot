"""
Base class for container-based (advanced) validators.

Advanced validators dispatch work to **validator jobs** — self-contained Docker
containers defined in the ``validibot-validators`` repository. The validator
class in this repo handles envelope building, job dispatch, callback processing,
and assertion evaluation; the actual domain-specific computation (running an
EnergyPlus simulation, executing an FMU) happens inside the container job.

All advanced validators follow the same lifecycle:

1. Validate run_context (must have validation_run and step)
2. Get the configured ExecutionBackend (Docker socket or Cloud Run)
3. Build an ExecutionRequest and call backend.execute()
4. For async backends: return pending result (passed=None)
5. For sync backends: process output envelope immediately
6. (On callback) Download and parse the output envelope
7. Extract issues from envelope messages
8. Extract output signals via ``extract_output_signals()``
9. Evaluate output-stage assertions against those signals
10. Return final ValidationResult with signals populated

Subclasses implement ``extract_output_signals()`` to handle their
validator-specific envelope structure. The base class handles the shared
orchestration, response conversion, and assertion evaluation.
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _

from validibot.validations.constants import Severity
from validibot.validations.validators.base.base import AssertionStats
from validibot.validations.validators.base.base import BaseValidator
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.base.base import ValidationResult

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.submissions.models import Submission
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Validator

logger = logging.getLogger(__name__)


class AdvancedValidator(BaseValidator):
    """
    Abstract base for container-based validators using the Template Method pattern.

    Advanced validators dispatch validation work to external container jobs
    (Docker containers defined in the ``validibot-validators`` repo) via the
    ExecutionBackend abstraction. This base class handles the shared lifecycle:

    - Validating that run_context is properly set
    - Getting the execution backend and building the request
    - Converting ExecutionResponse to ValidationResult
    - Processing output envelopes (extracting messages and signals)
    - Evaluating output-stage assertions

    Subclasses must implement:

    - ``extract_output_signals()`` — extract domain-specific signals from
      the output envelope for assertion evaluation

    The ``validate()`` and ``post_execute_validate()`` methods implement the
    template. Subclasses should not need to override them unless they require
    custom envelope processing beyond signal extraction.
    """

    @property
    @abstractmethod
    def validator_display_name(self) -> str:
        """Human-readable name for error messages (e.g., 'EnergyPlus', 'FMU')."""
        ...

    @classmethod
    @abstractmethod
    def extract_output_signals(cls, output_envelope: Any) -> dict[str, Any] | None:
        """
        Extract assertion signals from the validator-specific output envelope.

        Each advanced validator type produces outputs in its own Pydantic envelope
        structure (defined in validibot_shared). This method extracts the signals
        that can be referenced in output-stage assertions.

        Args:
            output_envelope: The typed Pydantic envelope from validibot_shared
                (e.g., EnergyPlusOutputEnvelope, FMUOutputEnvelope).

        Returns:
            Dict mapping catalog slugs to values for CEL evaluation, or None if
            no signals can be extracted.
        """
        ...

    # PUBLIC METHODS
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset | None,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """
        Dispatch validation to the configured execution backend.

        For sync backends (Docker Compose), returns results immediately.
        For async backends (Cloud Run, AWS Batch), returns a pending result
        (passed=None) and results arrive later via callback.

        Args:
            validator: The Validator model instance.
            submission: The Submission with file content.
            ruleset: Optional ruleset with assertions.
            run_context: Required execution context with validation_run and step.

        Returns:
            ValidationResult with passed=True/False for sync backends,
            passed=None (pending) for async backends, or passed=False on error.
        """
        self.run_context = run_context

        run = run_context.validation_run if run_context else None
        step = run_context.step if run_context else None

        if not run or not step:
            logger.error(
                "%s validator requires run_context to be set with "
                "validation_run and workflow_step",
                self.validator_display_name,
            )
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=_(
                            "%(name)s validation requires workflow context. "
                            "Ensure the validator is called via the workflow handler."
                        )
                        % {"name": self.validator_display_name},
                        severity=Severity.ERROR,
                    ),
                ],
                stats={"implementation_status": "Missing run_context"},
            )

        from validibot.validations.services.execution import get_execution_backend
        from validibot.validations.services.execution.base import ExecutionRequest

        backend = get_execution_backend()

        if not backend.is_available():
            logger.warning(
                "Execution backend '%s' is not available",
                backend.backend_name,
            )
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=_(
                            "Validation backend is not available. "
                            "Check your deployment configuration."
                        ),
                        severity=Severity.ERROR,
                    ),
                ],
                stats={"implementation_status": "Backend not available"},
            )

        request = ExecutionRequest(
            run=run,
            validator=validator,
            submission=submission,
            step=step,
        )

        response = backend.execute(request)
        return self._response_to_result(response, is_async=backend.is_async)

    def post_execute_validate(
        self,
        output_envelope: Any,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """
        Process container output and evaluate output-stage assertions.

        Called after container execution completes (either sync or via callback).
        This method:

        1. Extracts issues from envelope messages
        2. Extracts signals via ``extract_output_signals()``
        3. Evaluates output-stage assertions using those signals
        4. Returns ValidationResult with signals field populated

        Args:
            output_envelope: The typed Pydantic envelope from the validator
                container (e.g., EnergyPlusOutputEnvelope, FMUOutputEnvelope).
            run_context: Execution context for assertion evaluation.

        Returns:
            ValidationResult with output-stage issues, assertion_stats,
            and signals populated.
        """
        from validibot_shared.validations.envelopes import Severity as EnvelopeSeverity
        from validibot_shared.validations.envelopes import ValidationStatus

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

        # Extract signals from envelope for downstream steps and assertions
        signals = self.extract_output_signals(output_envelope) or {}

        # Evaluate output-stage assertions if we have context
        assertion_total = 0
        assertion_failures = 0
        if run_context and run_context.step:
            validator = run_context.step.validator
            ruleset = run_context.step.ruleset

            if validator and ruleset:
                assertion_result = self.evaluate_assertions_for_stage(
                    validator=validator,
                    ruleset=ruleset,
                    payload=signals,
                    stage="output",
                )
                issues.extend(assertion_result.issues)
                assertion_total = assertion_result.total
                assertion_failures = assertion_result.failures

        # Determine pass/fail based on envelope status and assertion failures.
        # Container error messages do not override SUCCESS status.
        if output_envelope.status == ValidationStatus.SUCCESS:
            passed = assertion_failures == 0
        elif output_envelope.status in (
            ValidationStatus.FAILED_VALIDATION,
            ValidationStatus.FAILED_RUNTIME,
        ):
            passed = False
        else:
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
                total=assertion_total,
                failures=assertion_failures,
            ),
            signals=signals,
            stats=stats,
        )

    # PRIVATE METHODS
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

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
        """
        stats = {
            "execution_id": response.execution_id,
            "input_uri": response.input_uri,
            "output_uri": response.output_uri,
            "execution_bundle_uri": response.execution_bundle_uri,
            "is_async": is_async,
        }
        if response.duration_seconds is not None:
            stats["duration_seconds"] = response.duration_seconds

        # Async execution — return pending result
        if is_async and not response.is_complete:
            return ValidationResult(passed=None, issues=[], stats=stats)

        # Error during execution
        if response.error_message:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message=response.error_message,
                        severity=Severity.ERROR,
                    ),
                ],
                stats=stats,
            )

        # No output envelope
        if response.output_envelope is None:
            return ValidationResult(
                passed=False,
                issues=[
                    ValidationIssue(
                        path="",
                        message="Validation completed but no output envelope received",
                        severity=Severity.ERROR,
                    ),
                ],
                stats=stats,
            )

        # Process the output envelope
        result = self._process_output_envelope(response.output_envelope, stats)
        # Attach envelope for processor to use in post_execute_validate()
        result.output_envelope = response.output_envelope
        return result

    def _process_output_envelope(
        self,
        envelope: Any,
        stats: dict,
    ) -> ValidationResult:
        """
        Process a ValidationOutputEnvelope and extract results.

        Used for sync backends where the output is available immediately.
        """
        from validibot_shared.validations.envelopes import Severity as EnvelopeSeverity
        from validibot_shared.validations.envelopes import ValidationStatus

        issues: list[ValidationIssue] = []

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

        if envelope.outputs:
            if hasattr(envelope.outputs, "model_dump"):
                stats["outputs"] = envelope.outputs.model_dump(mode="json")
            elif isinstance(envelope.outputs, dict):
                stats["outputs"] = envelope.outputs

        if envelope.status == ValidationStatus.SUCCESS:
            passed = True
        elif envelope.status in (
            ValidationStatus.FAILED_VALIDATION,
            ValidationStatus.FAILED_RUNTIME,
        ):
            passed = False
        else:
            passed = False

        return ValidationResult(passed=passed, issues=issues, stats=stats)
