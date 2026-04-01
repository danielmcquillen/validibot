"""
Base classes and data structures for validators.

A validator is a class that subclasses BaseValidator and implements the
validate() method. The subclass is what does the actual validation work
in a given validation step.

## Simple vs Advanced Validators

**Simple validators** (Basic, JSON Schema, XML Schema, AI, THERM) execute
validation inline and return complete results immediately. They evaluate
assertions during the validate() call. See ``simple.py`` for the template
method base class.

**Advanced validators** (EnergyPlus, FMU) launch container jobs and return
pending results. The job runs externally and POSTs results back via callback.
See ``advanced.py`` for the template method base class. For these validators:

1. validate() launches the job and returns passed=None (pending)
2. Container job executes and writes output envelope to storage
3. Job POSTs callback with result_uri to Django worker
4. Callback service downloads envelope and evaluates output-stage assertions

The container execution varies by deployment:
- Docker Compose: Docker containers (synchronous)
- GCP: Cloud Run Jobs (async with callbacks)
- AWS: AWS Batch (future)

## Output Envelopes and Assertion Signals

Each advanced validator type produces outputs in its own Pydantic envelope
structure (defined in validibot_shared). For example:

- EnergyPlus: outputs.metrics contains site_eui_kwh_m2, site_electricity_kwh, etc.
- FMU: outputs.output_values contains a dict keyed by catalog slug

To evaluate assertions after a container job completes, the callback service
needs to extract these signals from the envelope. Validators implement the
class method ``extract_output_signals()`` to handle their specific envelope
structure. This keeps envelope knowledge localized to the validator rather
than scattered across the callback service.

You won't find any concrete implementations here; those are in other modules.
"""

from __future__ import annotations

import logging
import re
from abc import ABC
from abc import abstractmethod
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from gettext import gettext as _
from typing import TYPE_CHECKING
from typing import Any

from validibot.validations.cel import DEFAULT_HELPERS
from validibot.validations.cel import CelHelper
from validibot.validations.constants import Severity
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import ValidationType

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.submissions.models import Submission
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Validator

logger = logging.getLogger(__name__)

# CEL requires top-level context variable names to be valid identifiers.
# Used for signal name validation at save time.
_CEL_IDENT_RE = re.compile(r"^[_a-zA-Z][_a-zA-Z0-9]*$")


def _is_valid_cel_identifier(name: str) -> bool:
    """Check whether *name* is a valid CEL identifier (signal name)."""
    return bool(_CEL_IDENT_RE.match(name))


@dataclass
class ValidationIssue:
    """
    Represents a single validation problem emitted by a validator.

    Attributes:
        path: JSON Pointer / XPath / dotted path for the failing value.
        message: Human readable description of the problem.
        severity: INFO/WARNING/ERROR (default ERROR).
        code: Optional machine-readable string for grouping (e.g. "json.required").
        meta: Optional loose metadata used to enrich ValidationFinding rows.
        assertion_id: Optional RulesetAssertion PK when the issue was produced
            by a structured assertion.
    """

    path: str
    message: str
    severity: Severity = Severity.ERROR
    code: str = ""
    meta: dict[str, Any] | None = None
    assertion_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AssertionStats:
    """
    Assertion evaluation statistics.

    Used by ValidationResult to track assertion counts in a structured way
    instead of loose dict keys.
    """

    total: int = 0
    failures: int = 0


@dataclass
class AssertionEvaluationResult:
    """
    Result of evaluating assertions for a stage.

    Bundles issues with total and failure counts so these values remain
    consistent and don't require duplicated counting logic in validators.
    """

    issues: list[ValidationIssue]
    total: int
    failures: int


@dataclass
class ValidationResult:
    """
    Aggregated result of a single validation step.

    Attributes:
        passed: True when no ERROR issues were produced. None indicates the
            validation is still pending (for async container-based validators).
        issues: List of issues discovered (may include INFO/WARNING).
        assertion_stats: Structured assertion counts (total and failures).
        signals: Extracted metrics for downstream steps. For advanced validators,
            this is populated by post_execute_validate() with output signals.
        output_envelope: For advanced validators, the typed container output
            envelope. Populated for sync execution; None for async.
        workflow_step_name: Slug of the workflow step that produced this result.
        stats: Additional validator-specific metadata (execution_id, URIs, timing).
    """

    passed: bool | None
    issues: list[ValidationIssue]
    assertion_stats: AssertionStats = field(default_factory=AssertionStats)
    signals: dict[str, Any] | None = None
    output_envelope: Any | None = None
    workflow_step_name: str | None = None  # slug
    stats: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "issues": [i.to_dict() for i in self.issues],
            "assertion_stats": {
                "total": self.assertion_stats.total,
                "failures": self.assertion_stats.failures,
            },
            "signals": self.signals or {},
            "stats": self.stats or {},
        }


class BaseValidator(ABC):
    """
    Base class for all validator implementations.

    Concrete subclasses should be registered in the registry keyed by
    ValidationType. Most validators extend one of the two template method
    subclasses instead of this class directly:

    - ``SimpleValidator`` for synchronous, inline validators
    - ``AdvancedValidator`` for validators requiring dedicated compute
      (container-based or compute-intensive services)

    Attributes:
        config: Arbitrary configuration dict (e.g., schema paths, thresholds, flags)

    The validate() method accepts an optional run_context argument containing:
        - validation_run: The ValidationRun model instance
        - step: The WorkflowStep model instance
        - downstream_signals: Signals from previous workflow steps (for CEL)

    Advanced validators (EnergyPlus, FMU) require run_context for job tracking.
    Simple validators (XML, JSON, Basic, AI, THERM) typically don't need it,
    though the base class CEL evaluation methods can use it for cross-step
    assertions.

    ## Implementing Advanced Validators

    Advanced validators that produce output envelopes should override
    ``extract_output_signals()`` to extract the signals dict from their
    envelope structure. This is used by the callback service to evaluate
    output-stage assertions after the container job completes.
    """

    validation_type: ValidationType
    cel_helpers = DEFAULT_HELPERS

    def __init__(self, *, config: dict[str, Any] | None = None) -> None:
        self.config: dict[str, Any] = config or {}
        self.processor_name: str = self.config.get("processor_name", "").strip()
        # run_context is now passed as an argument to validate(), but we keep
        # a reference on the instance for use by CEL evaluation methods.
        self.run_context: RunContext | None = None

    def get_cel_helpers(self) -> dict[str, CelHelper]:
        """
        Return the helper allowlist for CEL evaluation in this validator.

        CEL helpers are the set of extra functions/variables exposed to CEL
        expressions at evaluation time. They extend the CEL standard library
        with domain-specific utilities (for example, normalization, date/time
        helpers, or convenience predicates) that we explicitly allow.

        This method provides an allowlist so we control what CEL can access.
        Subclasses can override to append or remove helpers based on validator
        metadata or security requirements.
        """
        return dict(self.cel_helpers)

    # -------------------------------------------------------- Output signal extraction

    @classmethod
    def extract_output_signals(cls, output_envelope: Any) -> dict[str, Any] | None:
        """
        Extract assertion signals from an output envelope for assertion evaluation.

        Advanced validators (EnergyPlus, FMU) produce output envelopes with
        domain-specific structures containing simulation results. This method
        extracts the signals that can be referenced in output-stage assertions.

        Override this in subclasses to handle validator-specific envelope structures.
        The base implementation returns None (no signals available).

        Args:
            output_envelope: The typed Pydantic envelope from
                validibot_shared containing validation results
                (e.g., EnergyPlusOutputEnvelope).

        Returns:
            Dict mapping catalog slugs to values for CEL evaluation, or None if
            no signals can be extracted. Keys should match the validator's output
            catalog entry slugs (e.g., "site_eui_kwh_m2" for EnergyPlus).

        Example (EnergyPlus):
            The EnergyPlus envelope has outputs.metrics containing fields like
            site_eui_kwh_m2, site_electricity_kwh, etc. The override extracts
            these as: {"site_eui_kwh_m2": 75.2, "site_electricity_kwh": 12345, ...}

        Example (FMU):
            The FMU envelope has outputs.output_values already keyed by catalog
            slug: {"y": 1.0, "temperature": 293.15, ...}
        """
        return None

    # ------------------------------------------------------------------ CEL helpers

    def _resolve_path(self, data: Any, path: str | None) -> tuple[Any, bool]:
        """Resolve dotted / [index] paths into nested dict/list payloads.

        Delegates to the shared ``resolve_path()`` function in
        ``validations.services.path_resolution``. This method is kept
        as a thin wrapper so existing callers (``_build_cel_context``,
        subclasses) continue to work without changes.

        Returns ``(value, found_flag)``.
        """
        from validibot.validations.services.path_resolution import resolve_path

        return resolve_path(data, path)

    def _build_cel_context(
        self,
        payload: Any,
        validator: Validator,
        *,
        stage: str = "input",
    ) -> dict[str, Any]:
        """
        Build the namespaced CEL context for assertion evaluation.

        The context has four namespaces (plus two aliases):

        - ``p`` / ``payload`` — raw submission data (or validator output
          payload for output-stage assertions). Always present.
        - ``s`` / ``signals`` — author-defined signals from workflow-level
          signal mapping and promoted validator outputs. Populated from
          ``RunContext.workflow_signals`` and step-bound input signal
          bindings.
        - ``output`` — this step's declared output signals (from
          ``SignalDefinition`` with ``direction="output"``).
        - ``steps`` — validator outputs from completed upstream steps,
          accessible as ``steps.<step_key>.output.<name>``.

        Raw payload keys are **never promoted** to top-level CEL
        variables. Authors access raw data via ``p.key`` (or
        ``payload.key``) and signals via ``s.name`` (or
        ``signals.name``).
        """
        # ── Signals namespace (s / signals) ──────────────────────────
        signals_dict: dict[str, Any] = {}

        # 1. Workflow-level signals from RunContext (resolved at run start
        # from WorkflowSignalMapping rows against submission data).
        wf_signals = getattr(
            getattr(self, "run_context", None),
            "workflow_signals",
            None,
        )
        if isinstance(wf_signals, dict):
            signals_dict.update(wf_signals)

        # NOTE: Step-bound input signals (StepSignalBinding rows) are NOT
        # injected into the s.* namespace.  Per the ADR, validator inputs
        # feed the validator (FMU/EnergyPlus parameters), not CEL
        # expressions.  The s.* namespace only contains:
        # - Workflow-level signals (from WorkflowSignalMapping)
        # - Promoted validator outputs (from SignalDefinition.signal_name)
        #
        # Authors reference payload data via p.key and signals via s.name.
        # Step-bound input resolution (_resolve_bound_input_context) is
        # still used by the validator's internal parameter binding, but
        # those values are NOT exposed in CEL.

        # ── Output namespace (o / output) ────────────────────────────
        # For output-stage assertions, the payload IS the validator
        # output (e.g., FMU results). The full output dict is placed
        # under ``output`` so authors access values via ``output.key``
        # or ``o.key``.
        #
        # For input-stage, declared output signals are resolved from
        # the payload so ``output.name`` is available even during input
        # assertions (e.g., for cross-direction comparisons).
        output_dict: dict[str, Any] = {}
        if stage == "output" and isinstance(payload, dict):
            output_dict = payload
        else:
            # Input stage: resolve declared output signals
            for sig in validator.signal_definitions.filter(
                direction=SignalDirection.OUTPUT,
            ).only("contract_key"):
                value, found = self._resolve_path(payload, sig.contract_key)
                output_dict[sig.contract_key] = value if found else None

        # NOTE: Declared input signal definitions are NOT injected into
        # the s.* namespace.  They are validator inputs, not author-
        # defined signals.  See ADR-2026-03-31 terminology section.

        # ── Steps namespace (downstream step outputs) ────────────────
        steps_context: dict[str, Any] = {}
        run_summary = getattr(
            getattr(self, "run_context", None),
            "validation_run",
            None,
        )
        if isinstance(getattr(run_summary, "summary", None), dict):
            steps_context = run_summary.summary.get("steps", {}) or {}
        downstream_override = getattr(
            getattr(self, "run_context", None),
            "downstream_signals",
            None,
        )
        if isinstance(downstream_override, dict) and downstream_override:
            steps_context = downstream_override

        # ── Promoted output signals ──────────────────────────────────
        # Validator outputs with a non-empty signal_name are promoted
        # to the s namespace.  Reconstructed from completed step outputs
        # in the run summary + promotion definitions on SignalDefinition.
        if steps_context:
            self._inject_promoted_outputs(signals_dict, steps_context)

        # ── Assemble the context ─────────────────────────────────────
        # All namespace roots are always present (even if empty) so CEL
        # expressions can reference them without undefined-variable
        # errors.
        context: dict[str, Any] = {
            "p": payload,
            "payload": payload,
            "s": signals_dict,
            "signal": signals_dict,
            "o": output_dict,
            "output": output_dict,
            "steps": steps_context if steps_context else {},
        }
        return context

    def _inject_promoted_outputs(
        self,
        signals_dict: dict[str, Any],
        steps_context: dict[str, Any],
    ) -> None:
        """Inject promoted validator outputs into the signals namespace.

        Scans ``SignalDefinition`` rows with non-empty ``signal_name``
        across all steps in the current workflow, then looks up each
        output's value from the completed step outputs in the run
        summary.  If a value is found, it is injected into
        ``signals_dict`` under the promoted signal name.

        This runs on every step (not just once at run start) because
        promoted outputs are only available after the producing step
        completes.
        """
        step = getattr(getattr(self, "run_context", None), "step", None)
        if not step:
            return
        workflow = getattr(step, "workflow", None)
        if not workflow:
            return

        from validibot.validations.models import SignalDefinition

        promoted = (
            SignalDefinition.objects.filter(
                workflow_step__workflow=workflow,
                direction=SignalDirection.OUTPUT,
            )
            .exclude(signal_name="")
            .only("signal_name", "contract_key", "workflow_step__step_key")
            .select_related("workflow_step")
        )

        for sig in promoted:
            step_key = getattr(sig.workflow_step, "step_key", None)
            if not step_key or step_key not in steps_context:
                continue
            step_outputs = steps_context.get(step_key, {})
            # Handle both {"output": {...}} and flat dict formats
            if isinstance(step_outputs, dict) and "output" in step_outputs:
                outputs = step_outputs["output"]
            else:
                outputs = step_outputs
            if isinstance(outputs, dict) and sig.contract_key in outputs:
                signals_dict[sig.signal_name] = outputs[sig.contract_key]

    def _resolve_bound_input_context(self, payload: Any) -> dict[str, Any]:
        """Resolve input signals wired to the current workflow step.

        CEL expressions on simple validators can reference signal contract keys
        like ``emissivity`` even when the submission stores the value at a
        nested path such as ``ownedMember[0].ownedAttribute[1].defaultValue``.
        When a workflow step defines ``StepSignalBinding`` rows, resolve those
        bindings first and inject the resulting values into the CEL context.

        Missing bound inputs are surfaced as ``None`` so assertions can still
        use null checks instead of crashing on undefined identifiers.
        """
        step = getattr(getattr(self, "run_context", None), "step", None)
        if step is None:
            return {}

        from validibot.validations.constants import SignalDirection
        from validibot.validations.models import StepSignalBinding
        from validibot.validations.services.path_resolution import resolve_input_signal

        submission = getattr(
            getattr(self, "run_context", None),
            "validation_run",
            None,
        )
        submission = getattr(submission, "submission", None)
        submission_metadata = getattr(submission, "metadata", None) or {}
        upstream_signals = (
            getattr(
                getattr(self, "run_context", None),
                "downstream_signals",
                None,
            )
            or {}
        )

        bindings = (
            StepSignalBinding.objects.filter(
                workflow_step=step,
                signal_definition__direction=SignalDirection.INPUT,
            )
            .select_related("signal_definition")
            .order_by("signal_definition__order", "signal_definition__pk")
        )

        if not bindings.exists():
            return {}

        submission_data = payload if isinstance(payload, (dict, list)) else {}

        context: dict[str, Any] = {}
        for binding in bindings:
            resolved = resolve_input_signal(
                binding,
                submission_data=submission_data,
                submission_metadata=submission_metadata,
                upstream_signals=upstream_signals,
            )
            context[binding.signal_definition.contract_key] = (
                resolved.value if resolved.resolved else None
            )

        return context

    def _issue_from_assertion(
        self,
        assertion,
        path: str,
        message: str,
    ) -> ValidationIssue:
        return ValidationIssue(
            path=path,
            message=message,
            severity=assertion.severity,
            code=assertion.operator,
            meta={"ruleset_id": assertion.ruleset_id},
            assertion_id=getattr(assertion, "id", None),
        )

    def _count_assertion_failures(self, issues: list[ValidationIssue]) -> int:
        """
        Count assertion failures from a list of issues.

        An assertion failure is an issue with an assertion_id that has
        ERROR severity. WARNING/INFO assertions are still issues but are
        intentionally configured as non-blocking.
        """
        return sum(
            1
            for issue in issues
            if issue.assertion_id is not None and issue.severity == Severity.ERROR
        )

    def _should_emit_success_messages(self) -> bool:
        """Check if success messages should be emitted for passed assertions."""
        if not self.run_context or not self.run_context.step:
            return False
        return bool(getattr(self.run_context.step, "show_success_messages", False))

    def _maybe_success_issue(self, assertion) -> ValidationIssue | None:
        """
        Create a success issue if the assertion has a success_message or
        the step has show_success_messages enabled.
        """
        success_message = getattr(assertion, "success_message", "") or ""
        has_custom_message = bool(success_message.strip())
        show_success = self._should_emit_success_messages()

        if not has_custom_message and not show_success:
            return None

        if has_custom_message:
            message = success_message.strip()
        else:
            # Generate default success message
            target = getattr(assertion, "target_display", "") or ""
            condition = getattr(assertion, "condition_display", "") or ""
            if target and condition:
                message = _("Assertion passed: %(target)s %(condition)s") % {
                    "target": target,
                    "condition": condition,
                }
            elif target:
                message = _("Assertion passed: %(target)s") % {"target": target}
            else:
                message = _("Assertion passed.")

        return ValidationIssue(
            path="",
            message=message,
            severity=Severity.SUCCESS,
            code="assertion_passed",
            meta={"ruleset_id": assertion.ruleset_id},
            assertion_id=getattr(assertion, "id", None),
        )

    def _count_stage_assertions(
        self,
        ruleset,
        target_stage: str,
        *,
        default_ruleset=None,
    ) -> int:
        """
        Count ALL assertions that match the given stage.

        Includes assertions from both the default_ruleset (validator-level)
        and the step-level ruleset.

        Args:
            ruleset: The step-level Ruleset model instance (may be None).
            target_stage: "input" or "output".
            default_ruleset: The validator's default Ruleset (may be None).

        Returns:
            Count of assertions matching the stage.
        """
        count = 0
        for rs in (default_ruleset, ruleset):
            if not rs:
                continue
            for assertion in rs.assertions.all():
                if assertion.resolved_run_stage == target_stage:
                    count += 1
        return count

    def evaluate_assertions_for_stage(
        self,
        *,
        validator: Validator,
        ruleset: Ruleset | None,
        payload: Any,
        stage: str,
    ) -> AssertionEvaluationResult:
        """
        Evaluate all assertions for a given stage using the evaluator registry.

        This is the unified entry point for assertion evaluation. It merges
        assertions from two sources, evaluated in this order:

        1. **Default assertions** from ``validator.default_ruleset`` - these are
           validator-level assertions that always run regardless of step config.
        2. **Step assertions** from the ``ruleset`` parameter - these are
           per-step assertions configured by the workflow author.

        Both sets are evaluated in a single pass, with default assertions
        ordered first. Within each set, assertions are ordered by
        ``(order, pk)``.

        Args:
            validator: The Validator model instance.
            ruleset: The step-level Ruleset model instance (may be None).
            payload: The data to evaluate assertions against.
            stage: "input" or "output" - only assertions matching this stage
                are evaluated.

        Returns:
            AssertionEvaluationResult with issues, total count, and failure count.
        """
        default_ruleset = getattr(validator, "default_ruleset", None)
        if ruleset is None and default_ruleset is None:
            return AssertionEvaluationResult(issues=[], total=0, failures=0)

        # Import the evaluators package to ensure all evaluators are registered
        # via their @register_evaluator decorators before we look them up.
        import validibot.validations.assertions.evaluators  # noqa: F401
        from validibot.validations.assertions.evaluators.base import AssertionContext
        from validibot.validations.assertions.evaluators.registry import get_evaluator

        # Merge assertions: default_ruleset first, then step-level ruleset.
        # Default assertions always run and are evaluated first.
        stage_assertions: list = []
        for rs in (default_ruleset, ruleset):
            if rs is None:
                continue
            assertions = list(
                rs.assertions.all()
                .select_related("target_signal_definition")
                .order_by("order", "pk")
            )
            stage_assertions.extend(
                a for a in assertions if a.resolved_run_stage == stage
            )

        if not stage_assertions:
            return AssertionEvaluationResult(issues=[], total=0, failures=0)

        # Build evaluation context (CEL context is lazy-built on first use)
        context = AssertionContext(validator=validator, engine=self, stage=stage)

        issues: list[ValidationIssue] = []
        for assertion in stage_assertions:
            evaluator = get_evaluator(assertion.assertion_type)
            if not evaluator:
                logger.warning(
                    "No evaluator registered for assertion type: %s",
                    assertion.assertion_type,
                )
                continue

            assertion_issues = evaluator.evaluate(
                assertion=assertion,
                payload=payload,
                context=context,
            )
            issues.extend(assertion_issues)

        total = len(stage_assertions)
        failures = self._count_assertion_failures(issues)
        return AssertionEvaluationResult(
            issues=issues,
            total=total,
            failures=failures,
        )

    @abstractmethod
    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """
        Run validation on a submission using the given validator and ruleset.

        Args:
            validator: The Validator model instance defining validation behavior.
            submission: The Submission model instance containing data to validate.
            ruleset: The Ruleset model instance with validation rules/assertions.
            run_context: Optional execution context containing validation_run and
                step for advanced validators. Simple validators typically don't
                need this.

        Returns:
            ValidationResult with passed status, issues list, and optional stats.
        """
        raise NotImplementedError

    def post_execute_validate(
        self,
        output_envelope: Any,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """
        Process container output and evaluate output-stage assertions.

        Only called for advanced validators after container completion.
        Called in two scenarios:
        1. Sync execution: Immediately after validate() returns with envelope
        2. Async execution: When callback arrives with envelope

        Implementation should:
        1. Extract issues from envelope.messages
        2. Extract signals via extract_output_signals()
        3. Evaluate output-stage assertions using those signals
        4. Return ValidationResult with signals field populated

        Default implementation raises NotImplementedError. Advanced validators
        (EnergyPlus, FMU) must override this.

        Args:
            output_envelope: The typed Pydantic envelope from
                validibot_shared containing validation results
                (e.g., EnergyPlusOutputEnvelope).
            run_context: Optional execution context for CEL evaluation.

        Returns:
            ValidationResult with output-stage issues, assertion_stats,
            and signals populated. A SUCCESS status is treated as passed even
            if the envelope contains ERROR messages; output-stage assertion
            failures are handled separately by the processor.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support post_execute_validate(). "
            "This is required for advanced validators."
        )
