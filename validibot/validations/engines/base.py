"""
Base classes and data structures for validator engines.

A validator engine is a class that subclasses BaseValidatorEngine and implements
the validate() method. The subclass is what does the actual validation work
in a given validation step.

## Sync vs Async Engines

**Sync engines** (Basic, JSON Schema, XML Schema, AI) execute validation inline
and return complete results immediately. They evaluate assertions during the
validate() call.

**Async engines** (EnergyPlus, FMI) launch container jobs and return pending
results. The job runs externally and POSTs results back via callback. For these
engines:

1. validate() launches the job and returns passed=None (pending)
2. Container job executes and writes output envelope to storage
3. Job POSTs callback with result_uri to Django worker
4. Callback service downloads envelope and evaluates output-stage assertions

The container execution varies by deployment:
- Self-hosted: Docker containers (synchronous)
- GCP: Cloud Run Jobs (async with callbacks)
- AWS: AWS Batch (future)

## Output Envelopes and Assertion Signals

Each async validator type produces outputs in its own Pydantic envelope structure
(defined in vb_shared). For example:

- EnergyPlus: outputs.metrics contains site_eui_kwh_m2, site_electricity_kwh, etc.
- FMI: outputs.output_values contains a dict keyed by catalog slug

To evaluate assertions after a container job completes, the callback service needs
to extract these signals from the envelope. Engines implement the class method
`extract_output_signals()` to handle their specific envelope structure. This
keeps envelope knowledge localized to the engine rather than scattered across the
callback service.

You won't find any concrete implementations here; those are in other modules.
"""

from __future__ import annotations

import logging
from abc import ABC
from abc import abstractmethod
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from gettext import gettext as _
from typing import TYPE_CHECKING
from typing import Any

from django.conf import settings

from validibot.validations.cel import DEFAULT_HELPERS
from validibot.validations.cel import CelHelper
from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.submissions.models import Submission
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Validator

logger = logging.getLogger(__name__)


@dataclass
class ValidationIssue:
    """
    Represents a single validation problem emitted by an engine.

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
    consistent and don't require duplicated counting logic in engines.
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
        stats: Additional engine-specific metadata (execution_id, URIs, timing).
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


class BaseValidatorEngine(ABC):
    """
    Base class for all validator engine implementations.

    Concrete subclasses should be registered in the registry keyed by ValidationType.

    Attributes:
        config: Arbitrary configuration dict (e.g., schema paths, thresholds, flags)

    The validate() method accepts an optional run_context argument containing:
        - validation_run: The ValidationRun model instance
        - step: The WorkflowStep model instance
        - downstream_signals: Signals from previous workflow steps (for CEL)

    Async engines (EnergyPlus, FMI) require run_context for job tracking. Sync
    engines (XML, JSON, Basic, AI) typically don't need it, though the base class
    CEL evaluation methods can use it for cross-step assertions.

    ## Implementing Async Engines

    Async engines that produce output envelopes should override
    `extract_output_signals()` to extract the signals dict from their
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
        Return the helper allowlist for CEL evaluation in this engine.

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

        Async engines (EnergyPlus, FMI) produce output envelopes with domain-specific
        structures containing simulation results. This method extracts the signals
        that can be referenced in output-stage assertions.

        Override this in subclasses to handle validator-specific envelope structures.
        The base implementation returns None (no signals available).

        Args:
            output_envelope: The typed Pydantic envelope from vb_shared containing
                            validation results (e.g., EnergyPlusOutputEnvelope).

        Returns:
            Dict mapping catalog slugs to values for CEL evaluation, or None if
            no signals can be extracted. Keys should match the validator's output
            catalog entry slugs (e.g., "site_eui_kwh_m2" for EnergyPlus).

        Example (EnergyPlus):
            The EnergyPlus envelope has outputs.metrics containing fields like
            site_eui_kwh_m2, site_electricity_kwh, etc. The override extracts
            these as: {"site_eui_kwh_m2": 75.2, "site_electricity_kwh": 12345, ...}

        Example (FMI):
            The FMI envelope has outputs.output_values already keyed by catalog
            slug: {"y": 1.0, "temperature": 293.15, ...}
        """
        return None

    # ------------------------------------------------------------------ CEL helpers

    def _resolve_path(self, data: Any, path: str | None) -> tuple[Any, bool]:
        """
        Resolve dotted / [index] paths into nested dict/list payloads.
        Returns (value, found_flag).
        """
        if not path:
            return data, True
        current = data
        tokens = str(path or "").split(".")
        for token in tokens:
            if not token:
                continue
            if "[" in token and token.endswith("]"):
                key, index_part = token.split("[", 1)
                index_str = index_part.rstrip("]")
                if key:
                    if isinstance(current, dict) and key in current:
                        current = current[key]
                    else:
                        return None, False
                try:
                    position = int(index_str)
                except ValueError:
                    return None, False
                if isinstance(current, (list, tuple)) and 0 <= position < len(current):
                    current = current[position]
                else:
                    return None, False
            elif isinstance(current, dict) and token in current:
                current = current[token]
            else:
                return None, False
        return current, True

    def _build_cel_context(self, payload: Any, validator: Validator) -> dict[str, Any]:
        """
        Build a context mapping catalog entry slugs to values resolved from payload.
        Include the raw payload so expressions can reference it directly if needed.
        If run_context includes downstream signals from earlier steps, expose them
        under a namespaced ``steps`` key to support cross-step assertions.
        """
        context: dict[str, Any] = {"payload": payload}
        derived_enabled = getattr(settings, "ENABLE_DERIVED_SIGNALS", False)
        qs = validator.catalog_entries.all().only(
            "slug",
            "is_required",
            "entry_type",
            "run_stage",
        )
        if not derived_enabled:
            qs = qs.filter(entry_type=CatalogEntryType.SIGNAL)
        entries = list(qs)
        for entry in entries:
            value, found = self._resolve_path(payload, entry.slug)
            if found:
                if (
                    entry.entry_type == CatalogEntryType.SIGNAL
                    and entry.run_stage == CatalogRunStage.OUTPUT
                    and entry.slug in context
                ):
                    # Preserve existing input mapping; expose output via
                    # prefix for disambiguation.
                    context.setdefault(f"output.{entry.slug}", value)
                else:
                    context[entry.slug] = value
                if (
                    entry.entry_type == CatalogEntryType.SIGNAL
                    and entry.run_stage == CatalogRunStage.OUTPUT
                ):
                    context.setdefault(f"output.{entry.slug}", value)
            elif entry.is_required:
                context[entry.slug] = None

        # Surface downstream signals for CEL expressions 
        # (e.g., steps.<id>.signals.<slug>).
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
        if steps_context:
            context["steps"] = steps_context

        def _collect_matches(data: Any, key: str) -> list[Any]:
            matches: list[Any] = []
            if isinstance(data, dict):
                for k, v in data.items():
                    if k == key:
                        matches.append(v)
                    matches.extend(_collect_matches(v, key))
            elif isinstance(data, list):
                for item in data:
                    matches.extend(_collect_matches(item, key))
            return matches

        if getattr(validator, "allow_custom_assertion_targets", False):
            if isinstance(payload, dict):
                for k, v in payload.items():
                    context.setdefault(k, v)
            # support lightweight partial path matches: if an identifier appears
            # exactly once anywhere in the payload tree, expose it directly.
            identifiers = set(context.keys())
            if isinstance(payload, (dict, list)):
                for key in list(payload.keys()) if isinstance(payload, dict) else []:
                    identifiers.add(key)
                for ident in identifiers:
                    matches = _collect_matches(payload, ident)
                    if len(matches) == 1 and ident not in context:
                        context[ident] = matches[0]
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

    def _count_stage_assertions(self, ruleset, target_stage: str) -> int:
        """
        Count ALL assertions that match the given stage.

        The stage is determined by each assertion's resolved_run_stage property,
        which uses target_catalog_entry.run_stage if set, otherwise defaults
        to OUTPUT.

        This counts all assertion types (CEL, basic, etc.) for the given stage.

        Args:
            ruleset: The Ruleset model instance (may be None).
            target_stage: "input" or "output".

        Returns:
            Count of assertions matching the stage.
        """
        if not ruleset:
            return 0

        # Count ALL assertion types, not just CEL
        assertions = ruleset.assertions.all()
        count = 0
        for assertion in assertions:
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

        This is the unified entry point for assertion evaluation. It iterates
        through assertions in order, dispatching each to the appropriate evaluator
        based on assertion_type. This allows mixed assertion types (BASIC, CEL,
        future types) to be evaluated in a single ordered pass.

        Args:
            validator: The Validator model instance.
            ruleset: The Ruleset model instance (may be None).
            payload: The data to evaluate assertions against.
            stage: "input" or "output" - only assertions matching this stage
                are evaluated.

        Returns:
            AssertionEvaluationResult with issues, total count, and failure count.
        """
        if ruleset is None:
            return AssertionEvaluationResult(issues=[], total=0, failures=0)

        from validibot.validations.assertions.evaluators.base import AssertionContext
        from validibot.validations.assertions.evaluators.registry import get_evaluator

        # Get all assertions ordered by (order, pk)
        assertions = list(
            ruleset.assertions.all()
            .select_related("target_catalog_entry")
            .order_by("order", "pk")
        )

        # Filter to target stage
        stage_assertions = [
            a for a in assertions if a.resolved_run_stage == stage
        ]

        if not stage_assertions:
            return AssertionEvaluationResult(issues=[], total=0, failures=0)

        # Build evaluation context (CEL context is lazy-built on first use)
        context = AssertionContext(validator=validator, engine=self)

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
                step for async engines. Sync engines typically don't need this.

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

        Default implementation raises NotImplementedError. Advanced engines
        (EnergyPlus, FMI) must override this.

        Args:
            output_envelope: The typed Pydantic envelope from vb_shared containing
                validation results (e.g., EnergyPlusOutputEnvelope).
            run_context: Optional execution context for CEL evaluation.

        Returns:
            ValidationResult with output-stage issues, assertion_stats,
            and signals populated.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support post_execute_validate(). "
            "This is required for advanced validators."
        )
