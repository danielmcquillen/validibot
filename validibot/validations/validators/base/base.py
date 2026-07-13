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
instance method ``extract_output_signals()`` to handle their specific envelope
structure. This keeps envelope knowledge localized to the validator rather
than scattered across the callback service. Instance method (not classmethod)
so subclasses can reach ``self.run_context`` for catalog-version-scoped
output filtering — see ``EnergyPlusValidator._filter_to_catalog_outputs``.

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
from validibot.validations.services.submission_context import (
    build_submission_assertion_context,
)

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

    # ------------------------------------- Step input/output signal extraction

    def extract_input_signals(self, payload: Any) -> dict[str, Any] | None:
        """
        Extract input-stage step inputs from the submission payload.

        Validators that parse an arcane payload format (EnergyPlus IDF,
        future codecs for IFC / gbXML / etc.) override this to expose
        parsed facts. The returned dict is keyed by catalog
        ``contract_key`` and populates the ``i.*`` namespace in CEL
        assertions evaluated at input stage.

        Most validators don't parse — schema validators check structure
        directly, parser-evaluation validators do their work in one pass
        and emit outputs only. The base default returns None, which
        leaves ``i.*`` empty for those validators (honest reflection of
        the validator complexity spectrum from ADR-2026-05-22b).

        Called after ``preprocess_submission()`` so template-mode
        submissions are parsed against the resolved payload.

        This is an instance method (not a classmethod, despite the
        original POC contract) so subclasses can reach
        ``self.run_context`` to look up validator-side artifacts that
        carry parser-fact provenance — e.g., ``FMUValidator`` reads
        from ``self.run_context.step.validator.fmu_model.introspection_metadata``
        because the FMU itself is bound to the validator, not the
        submission. All existing call sites already invoke this via
        ``self.extract_input_signals(payload)`` so the conversion is
        backwards-compatible.

        Args:
            payload: The submission payload.

        Returns:
            Dict mapping catalog ``contract_key`` to extracted values,
            or None.
        """
        return None

    def extract_output_signals(self, output_envelope: Any) -> dict[str, Any] | None:
        """
        Extract assertion signals from an output envelope for assertion evaluation.

        Advanced validators (EnergyPlus, FMU) produce output envelopes with
        domain-specific structures containing simulation results. This method
        extracts the signals that can be referenced in output-stage assertions.

        Override this in subclasses to handle validator-specific envelope
        structures. The base implementation returns None (no signals
        available).

        This is an instance method (not a classmethod) so subclasses can
        reach ``self.run_context`` to scope catalog lookups against the
        exact validator row bound to the current step — important when
        multiple catalog versions co-exist in the database.

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

        Per ADR-2026-05-22b (five namespaces), ADR-2026-06-03b
        (the sixth, ``submission``), and ADR-2026-06-18 (the seventh,
        ``c`` / ``const``), the context has SEVEN namespaces,
        five with short/long aliases. Singular long names are used
        throughout (``payload``, ``signal``, ``input``, ``output``) —
        each namespace is a singular "thing" from the author's
        perspective, even when the underlying dict holds many
        values. ``steps`` and ``submission`` are the exceptions:
        ``steps`` is a collection of per-step records, and
        ``submission`` is long-only because ``s`` already means
        ``signal``.

        - ``p`` / ``payload`` — raw submission data (or validator
          output payload for output-stage assertions). Always
          present.
        - ``s`` / ``signal`` — workflow vocabulary. Populated from
          ``RunContext.workflow_signals`` (the
          ``WorkflowSignalMapping`` rows resolved at run start) AND
          from upstream promotions injected by
          ``_inject_promotions`` (both in-row
          ``promoted_signal_name`` on step-owned ``StepIODefinition``
          rows and the ``WorkflowStepIOPromotion`` overlay rows on
          validator-owned ones). Per ADR-2026-05-22b's symmetric
          promotion, both INPUT- and OUTPUT-direction definitions
          can promote.
        - ``i`` / ``input`` — step-local input values for the
          current step. Populated from parser-extracted facts
          (``extract_input_signals``) and resolved
          ``StepInputBinding`` rows. **Step inputs live here, not
          in s.*** — the form's CEL identifier validator rejects
          ``s.<step_input>`` references because they would silently
          read null.
        - ``o`` / ``output`` — this step's declared output values
          (from ``StepIODefinition`` rows with ``direction="output"``,
          populated by ``extract_output_signals``).
        - ``steps`` — both inputs and outputs from completed upstream
          steps, accessible as ``steps.<step_key>.input.<name>`` and
          ``steps.<step_key>.output.<name>``.
        - ``submission`` — the submission *envelope* (NOT the file
          content): submitter-set fields (``submission.name``,
          ``submission.short_description``, the free-form
          ``submission.metadata.<key>`` bag) plus server-stamped facts
          (``submission.file_type``, ``submission.size``,
          ``submission.uploaded_at``). Assembled by
          ``build_submission_assertion_context`` from
          ``run_context.validation_run`` and present at BOTH input and
          output stages because the envelope is fixed at submission
          time. Long-only (no short alias). Unlike ``s.*``/``p.*`` it
          resolves identically for any file format — the one namespace
          usable in a SHACL/``.ttl`` workflow. Resolves to ``{}`` when
          there is no run/submission (e.g. unit tests).
        - ``c`` / ``const`` — author-defined Constants from the workflow
          definition (ADR-2026-06-18): fixed literals known at authoring
          time, carried as a literal map on
          ``RunContext.workflow_constants``. Always present, never
          resolved, identical at input and output stage.

        Raw payload keys are **never promoted** to top-level CEL
        variables. Authors access raw data via ``p.key`` (or
        ``payload.key``) and signals via ``s.name`` (or
        ``signal.name``).
        """
        # ── Signals namespace (s / signal) ───────────────────────────
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

        # NOTE: Step inputs (whether parser-extracted or
        # StepInputBinding-resolved) are NOT injected into the s.*
        # namespace — they live in the dedicated i.* namespace
        # (populated further down in this method). The s.* namespace
        # ONLY contains:
        #
        # - Workflow-level signals (from WorkflowSignalMapping)
        # - Promoted step inputs and outputs, both step-owned (via the
        #   in-row ``promoted_signal_name`` field) and validator-owned
        #   (via the WorkflowStepIOPromotion overlay table) — see
        #   ``_inject_promotions`` below.
        #
        # Authors reference payload data via p.key, workflow vocabulary
        # via s.name, and step-local input/output values via i.name /
        # o.name. The form-side CEL identifier validator rejects
        # s.<step_input> references that would resolve to null at
        # runtime (see ``_validate_cel_identifiers``).

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
        # the s.* namespace. They are step inputs (validator-defined
        # contracts), not author-defined signals. Step inputs live in
        # the dedicated i.* namespace built below.

        # ── Input namespace (i / input) ──────────────────────────────
        # Step-local input values available at the start of the step.
        # Populated from two sources:
        #   1. Parser-extracted facts via extract_input_signals() —
        #      validators that parse an arcane format (EnergyPlus IDF)
        #      OR read stamped metadata (FMU's introspection dict)
        #      override the hook to return a dict keyed by catalog
        #      contract_key.
        #   2. Pre-resolved StepInputBinding values (contract-keyed)
        #      cached on the run context by the input-resolution
        #      pipeline (see resolve_step_input_signals).
        #
        # For built-in validators that don't parse and don't take
        # bindings (JSON Schema, XML Schema, Basic), i.* stays empty
        # — that's the honest reflection per the validator complexity
        # spectrum from ADR-2026-05-22b.
        inputs_dict: dict[str, Any] = {}

        # Pre-resolved contract-keyed binding values (set by the input
        # resolution pipeline before the container runs). See ADR-2026-05-22.
        bound_contract_values = getattr(
            getattr(self, "run_context", None),
            "step_input_contract_values",
            None,
        )
        if isinstance(bound_contract_values, dict):
            inputs_dict.update(bound_contract_values)

        # Parser-extracted facts via the validator's extract_input_signals
        # classmethod. Default returns None (most validators don't parse).
        # Called only at input stage where the payload is the raw submission.
        # At output stage the payload is the validator output envelope, not
        # the original submission, so we don't re-parse.
        if stage == "input":
            try:
                parsed = self.extract_input_signals(payload)
            except Exception:
                # Parser failures must not break assertion evaluation —
                # they surface as findings via the validator's normal
                # error path. The i.* namespace stays empty for the
                # signals the parser couldn't produce.
                parsed = None
            if isinstance(parsed, dict):
                # Parser facts win over bindings only when not already set —
                # explicit bindings take precedence over implicit parser
                # extraction (the author opted into the binding).
                for key, value in parsed.items():
                    inputs_dict.setdefault(key, value)

        # ── Steps namespace (upstream step inputs and outputs) ───────
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

        # ── Promoted step inputs and outputs ─────────────────────────
        # Step inputs/outputs with a non-empty promoted_signal_name
        # (in-row for step-owned definitions OR overlay row for
        # validator-owned ones) are surfaced in the s namespace.
        # Reconstructed from completed upstream step inputs/outputs
        # in the run summary; handles both directions per
        # ADR-2026-05-22b's symmetric promotion.
        if steps_context:
            self._inject_promotions(signals_dict, steps_context)

        # ── Submission envelope namespace (submission) ───────────────
        # The submitter metadata + server facts that live BESIDE the file.
        # Built once by the shared builder so CEL and basic assertions (and
        # the tests) see byte-identical data. Resolved from the run on the
        # run context; ``{}`` when there is no run/submission (e.g. a unit
        # test that calls _build_cel_context without a run context). Present
        # at both input and output stages — the envelope is fixed at
        # submission time, so it has no stage of its own.
        run = getattr(getattr(self, "run_context", None), "validation_run", None)
        submission_dict = build_submission_assertion_context(run)

        # ── Constants namespace (c / const) ──────────────────────────
        # Author-defined fixed literals from the workflow definition
        # (ADR-2026-06-18). A literal map built once at run start and carried on
        # the run context — no resolution, always present, the only namespace
        # whose values are known at authoring time. Bound under both ``c`` and
        # ``const`` (alias), like ``s`` / ``signal``.
        constants_dict = (
            getattr(
                getattr(self, "run_context", None),
                "workflow_constants",
                None,
            )
            or {}
        )

        # ── Assemble the context ─────────────────────────────────────
        # All namespace roots are always present (even if empty) so CEL
        # expressions can reference them without undefined-variable
        # errors. Six namespaces, four with their long-form aliases:
        #   p / payload   — raw submission file data
        #   s / signal    — workflow vocabulary
        #   i / input     — step-local input-stage values
        #   o / output    — step-local output-stage values
        #   steps         — cross-step inputs and outputs
        #   submission    — submission envelope (metadata + server facts)
        #
        # The keys here ARE the canonical namespace roots. The set is the
        # single source of truth ``CEL_NAMESPACE_ROOTS`` (validations/cel.py),
        # from which every authoring-time allowlist derives. This dict can't
        # derive from that set directly (each root maps to a different value
        # object), so the coupling is enforced by the canary test
        # ``test_context_root_keys_are_fixed``: it asserts these keys equal
        # ``CEL_NAMESPACE_ROOTS``, failing if either side gains a namespace
        # the other lacks.
        context: dict[str, Any] = {
            "p": payload,
            "payload": payload,
            "s": signals_dict,
            "signal": signals_dict,
            "i": inputs_dict,
            "input": inputs_dict,
            "o": output_dict,
            "output": output_dict,
            "steps": steps_context if steps_context else {},
            "submission": submission_dict,
            "c": constants_dict,
            "const": constants_dict,
        }
        return context

    def _inject_promotions(
        self,
        signals_dict: dict[str, Any],
        steps_context: dict[str, Any],
    ) -> None:
        """Inject promoted step inputs and outputs into the ``s.*`` namespace.

        Scans two sources of workflow-scoped promotions across all
        upstream steps in the current workflow and injects their
        resolved values into ``signals_dict``:

        1. **In-row** promotions on step-owned ``StepIODefinition``
           rows (the ``promoted_signal_name`` field).
        2. **Overlay** promotions on validator-owned rows
           (``WorkflowStepIOPromotion`` rows keyed on
           ``(workflow_step, signal_definition)``).

        Both sources read from completed upstream steps in
        ``run.summary["steps"]``, but the **direction** of the
        promotion picks which subkey to read from:

        - INPUT-direction promotions read from
          ``run.summary["steps"][step_key]["input"][contract_key]``
          — populated by the producing step's input stage from
          parser facts and resolved bindings.
        - OUTPUT-direction promotions read from
          ``run.summary["steps"][step_key]["output"][contract_key]``
          — populated after the producing step's container or
          inline work completes.

        Per ADR-2026-05-22b's symmetric promotion this handles INPUT
        and OUTPUT directions uniformly (earlier revisions handled
        only outputs).

        Runs on every step (not just once at run start) because
        promoted values only become available after each producing
        step completes its relevant stage. The downstream-only
        filter (``workflow_step__order__lt=current_step.order``)
        enforces the ADR's temporal rule — a step never sees its
        own promotion.
        """
        step = getattr(getattr(self, "run_context", None), "step", None)
        if not step:
            return
        workflow = getattr(step, "workflow", None)
        if not workflow:
            return

        from validibot.validations.models import StepIODefinition
        from validibot.validations.models import WorkflowStepIOPromotion

        # Per ADR-2026-05-22b's symmetric promotion: ANY direction
        # StepIODefinition with a non-empty promoted_signal_name
        # promotes its value into the s.* workflow vocabulary.
        # Previously this query filtered to direction=OUTPUT, which
        # silently broke input promotion. Reading both directions
        # makes input promotion actually work.
        #
        # ── Downstream-only rule (ADR-2026-05-22b temporal rule) ──
        # Promoted values are visible ONLY in steps that begin
        # execution after the producing step completes its relevant
        # stage. Without this filter, a step could see its own
        # promoted input via s.<name> during its own assertion
        # evaluation — because i.* is persisted to run.summary during
        # the producing step's input stage, and this query would
        # match the producing step's promoted definitions. That would
        # violate the ADR's "inside the producing step use i./o.;
        # downstream use s." rule. The order__lt filter restricts
        # injection to definitions on upstream steps only.
        #
        # ── Two promotion sources (May 2026 P1 fix) ──
        # Promotions live in two tables to handle two ownership
        # patterns of StepIODefinition rows:
        #
        # 1. Step-owned rows store the promoted_signal_name in-row.
        #    Query: StepIODefinition with workflow_step matching an
        #    upstream step in this workflow.
        # 2. Validator-owned rows (catalog entries shared across
        #    workflows) can't carry a workflow-scoped name in-row,
        #    so a WorkflowStepIOPromotion overlay holds the name
        #    per (workflow_step, signal_definition). Query the
        #    overlay table for upstream steps in this workflow.
        #
        # The two queries are run separately and their results merged
        # into the same loop, ordered by upstream step.
        current_step_order = getattr(step, "order", None)

        # Source 1: in-row promotions on step-owned rows.
        step_owned_qs = StepIODefinition.objects.filter(
            workflow_step__workflow=workflow,
        ).exclude(promoted_signal_name="")
        if current_step_order is not None:
            step_owned_qs = step_owned_qs.filter(
                workflow_step__order__lt=current_step_order,
            )
        step_owned = step_owned_qs.only(
            "promoted_signal_name",
            "contract_key",
            "direction",
            "workflow_step__step_key",
        ).select_related("workflow_step")

        # Source 2: overlay promotions on validator-owned rows.
        overlay_qs = WorkflowStepIOPromotion.objects.filter(
            workflow_step__workflow=workflow,
        )
        if current_step_order is not None:
            overlay_qs = overlay_qs.filter(
                workflow_step__order__lt=current_step_order,
            )
        overlays = overlay_qs.only(
            "promoted_signal_name",
            "signal_definition__contract_key",
            "signal_definition__direction",
            "workflow_step__step_key",
        ).select_related("workflow_step", "signal_definition")

        # Build a unified iterable of (promoted_name, contract_key,
        # direction, step_key) tuples so the injection loop below
        # doesn't care which source the promotion came from.
        promotions: list[tuple[str, str, str, str | None]] = []
        for sig in step_owned:
            promotions.append(
                (
                    sig.promoted_signal_name,
                    sig.contract_key,
                    sig.direction,
                    getattr(sig.workflow_step, "step_key", None),
                ),
            )
        for overlay in overlays:
            promotions.append(
                (
                    overlay.promoted_signal_name,
                    overlay.signal_definition.contract_key,
                    overlay.signal_definition.direction,
                    getattr(overlay.workflow_step, "step_key", None),
                ),
            )

        for promoted_name, contract_key, direction, step_key in promotions:
            if not step_key or step_key not in steps_context:
                continue
            step_data = steps_context.get(step_key, {})
            # Per ADR-2026-05-22, run.summary["steps"][key] holds both
            # "input" (step inputs from extract_input_signals + bindings)
            # and "output" (step outputs from extract_output_signals).
            # Read from the subkey that matches the promoted signal's
            # direction. The legacy flat-dict format is treated as
            # output for backward compatibility with pre-ADR runs.
            if isinstance(step_data, dict):
                if direction == SignalDirection.INPUT:
                    values = step_data.get("input", {})
                elif "output" in step_data:
                    values = step_data["output"]
                else:
                    # Legacy flat-dict format from before ADR-2026-05-22
                    # — treat it as output.
                    values = step_data
            else:
                values = {}
            if isinstance(values, dict) and contract_key in values:
                signals_dict[promoted_name] = values[contract_key]

    def _resolve_bound_input_context(self, payload: Any) -> dict[str, Any]:
        """Resolve input signals wired to the current workflow step.

        CEL expressions on simple validators can reference signal contract keys
        like ``emissivity`` even when the submission stores the value at a
        nested path such as ``ownedMember[0].ownedAttribute[1].defaultValue``.
        When a workflow step defines ``StepInputBinding`` rows, resolve those
        bindings first and inject the resulting values into the CEL context.

        Missing bound inputs are surfaced as ``None`` so assertions can still
        use null checks instead of crashing on undefined identifiers.
        """
        step = getattr(getattr(self, "run_context", None), "step", None)
        if step is None:
            return {}

        from validibot.validations.constants import SignalDirection
        from validibot.validations.models import StepInputBinding
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
        # Workflow-level signals (the s.* namespace) must be passed to
        # the resolver too — a StepInputBinding with
        # source_scope=SIGNAL reads from this dict via
        # resolve_input_signal. Without this argument, every
        # signal-sourced binding silently resolves to its default (or
        # marks "unresolved"), and i.<contract_key> ends up null even
        # when the workflow signal is present. Per the May 2026 P2
        # review finding.
        workflow_signals = (
            getattr(
                getattr(self, "run_context", None),
                "workflow_signals",
                None,
            )
            or {}
        )

        bindings = (
            StepInputBinding.objects.filter(
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
                workflow_signals=workflow_signals,
            )
            context[binding.signal_definition.contract_key] = (
                resolved.value if resolved.resolved else None
            )

        return context

    def _enrich_basic_payload(
        self,
        base_payload: Any,
        *,
        stage: str,
        output_signals: dict[str, Any] | None = None,
    ) -> Any:
        """Merge namespaced values into a BASIC-evaluator payload.

        BASIC evaluators walk a dotted path against the payload
        root — they don't understand the ``i.*`` / ``s.*`` / ``o.*``
        CEL namespaces directly. To make BASIC assertions work
        against namespaced targets without changing the evaluator,
        we merge the resolved values into the payload at the top
        level by their bare contract_key (for i.*/o.*) or workflow
        signal name (for s.*).

        That way a BASIC assertion whose
        ``target_signal_definition.contract_key`` is ``temperature``
        finds the value at ``payload["temperature"]`` regardless of
        whether it came from a parser fact, a resolved
        ``StepInputBinding``, a workflow signal mapping, or an
        extracted output envelope.

        Merge order (base wins on collision):

        1. The base payload (raw submission or extracted signals dict).
           Its keys win on collision because the submission shape
           should not be silently shadowed by a same-named binding.
        2. Workflow signals (``run_context.workflow_signals``).
        3. Resolved input bindings — only at input stage. Output
           stage already has ``resolved_inputs`` merged by
           ``_build_assertion_payload`` on the advanced side; calling
           ``_resolve_bound_input_context`` again at output stage
           would be redundant.
        4. Output signals — only when supplied (output stage on
           advanced validators).
        5. The ``submission`` envelope — a NESTED sub-dict (not
           flattened to bare keys), so a target like
           ``submission.metadata.deliverable`` walks naturally. Per
           ADR-2026-06-03b it is injected last and authoritatively
           (it overwrites any same-named payload key), because
           ``submission`` is a reserved namespace and a
           ``submission.*`` target must read the envelope, never a
           file key that happens to be called ``submission``. This is
           the basic-path analogue of CEL's separate top-level
           ``submission`` context key.

        Non-dict payloads (lxml ElementTree from XML Schema, RDF
        graphs from SHACL/``.ttl``) cannot be walked or merged into,
        but the INJECTABLE namespaces (``s.*``, ``o.*`` and
        ``submission``) live beside the file and must still resolve.
        For those we therefore return a MINIMAL enriched dict
        carrying just those namespaces — so a metadata-only basic
        assertion on a ``.ttl`` submission sees the envelope. ``p.*``
        stays unavailable for them (the raw content isn't walkable),
        which is unchanged behaviour.

        Args:
            base_payload: The validator-specific base payload
                (typically the signals dict from a simple validator's
                ``extract_signals`` or the assertion payload from an
                advanced validator).
            stage: ``"input"`` or ``"output"`` — controls whether
                StepInputBinding resolution runs.
            output_signals: At output stage, the extracted output
                dict to merge into the payload. None at input stage.

        Returns:
            The enriched dict — either the dict payload with the
            namespaces merged in, or (for a non-dict base) a minimal
            dict carrying only the injectable namespaces.
        """
        # For a non-dict base (XML Schema ElementTree, SHACL/THERM RDF graph) we
        # can't walk or merge the parsed object, so we start from an empty dict
        # and inject only the namespaces that live beside the file (s.*/o.*/
        # submission). ``p.*`` (the raw content) stays unavailable for those —
        # unchanged behaviour.
        enriched = dict(base_payload) if isinstance(base_payload, dict) else {}

        # Workflow signals — the s.* namespace's runtime values.
        workflow_signals = (
            getattr(
                getattr(self, "run_context", None),
                "workflow_signals",
                None,
            )
            or {}
        )
        for key, value in workflow_signals.items():
            enriched.setdefault(key, value)

        # Resolved input bindings — i.* values via StepInputBinding
        # source-path resolution. Only at input stage, and only for a dict
        # base (binding resolution walks the submission payload).
        if stage == "input" and isinstance(base_payload, dict):
            try:
                bound = self._resolve_bound_input_context(base_payload)
            except Exception:
                logger.debug(
                    "Could not resolve bound input context for BASIC "
                    "payload enrichment; falling back to base payload.",
                    exc_info=True,
                )
                bound = {}
            for key, value in bound.items():
                enriched.setdefault(key, value)

        # Output signals — o.* values from extract_output_signals.
        # Only at output stage.
        if stage == "output" and output_signals:
            for key, value in output_signals.items():
                enriched.setdefault(key, value)

        # Submission envelope — injected last as a nested, authoritative
        # sub-dict (see merge-order note above). Built by the shared builder
        # so basic and CEL see byte-identical data; ``{}`` when there is no
        # run/submission. Available at both stages because the envelope is
        # fixed at submission time.
        run = getattr(getattr(self, "run_context", None), "validation_run", None)
        enriched["submission"] = build_submission_assertion_context(run)

        # Constants envelope (c / const) — injected as a NESTED sub-dict, exactly
        # like ``submission`` and explicitly NOT flattened to bare keys the way
        # s.* signals are (ADR-2026-06-18). This is what makes ``c.energy_price``
        # and a bare-key signal ``energy_price`` coexist without colliding: a
        # Basic target ``c.energy_price`` resolves to ``payload["c"]["price"]``
        # while the signal flattens to ``payload["energy_price"]``. Authoritative
        # (overwrites any same-named payload key) because ``c``/``const`` are
        # reserved namespace roots. Both spellings bound for parity with CEL.
        constants_dict = (
            getattr(
                getattr(self, "run_context", None),
                "workflow_constants",
                None,
            )
            or {}
        )
        enriched["c"] = constants_dict
        enriched["const"] = constants_dict

        return enriched

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

    def _maybe_success_issue(
        self,
        assertion,
        *,
        template_context: dict[str, Any] | None = None,
    ) -> ValidationIssue | None:
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
            if template_context is not None:
                from validibot.validations.assertions.message_templates import (
                    MessageTemplateRenderError,
                )
                from validibot.validations.assertions.message_templates import (
                    render_assertion_message_template,
                )

                try:
                    rendered = render_assertion_message_template(
                        message,
                        template_context,
                    )
                except MessageTemplateRenderError:
                    rendered = ""
                if rendered:
                    message = rendered
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
        exclude_assertion_types: set[str] | None = None,
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
            exclude_assertion_types: Assertion types already handled by the
                caller and intentionally skipped by the generic evaluator.

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

        excluded = set(exclude_assertion_types or ())

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
                a
                for a in assertions
                if a.resolved_run_stage == stage
                and a.assertion_type not in excluded
                # Tabular row/column assertions reference row.*/col.*, which the
                # generic stage context does not bind — the TabularValidator
                # owns their evaluation (per ADR-2026-05-26's persistence
                # decision), so skip them here regardless of stage.
                and (a.options or {}).get("tabular_stage") not in {"row", "column"}
            )

        if not stage_assertions:
            return AssertionEvaluationResult(issues=[], total=0, failures=0)

        # Build evaluation context (CEL context is lazy-built on first use).
        # Pin CEL now() to the run's started_at so a time-relative assertion is
        # deterministic for the run; without a run context now() stays unbound
        # and any expression using it fails cleanly (never the wall clock). This
        # is what makes now() actually usable in generic (Basic/JSON/XML/tabular
        # dataset) assertions — the authoring allowlist accepts it, and this
        # binds it at runtime so it no longer fails every run.
        run = getattr(getattr(self, "run_context", None), "validation_run", None)
        context = AssertionContext(
            validator=validator,
            engine=self,
            stage=stage,
            now=getattr(run, "started_at", None),
        )

        issues: list[ValidationIssue] = []
        evaluated_total = 0
        for assertion in stage_assertions:
            evaluator = get_evaluator(assertion.assertion_type)
            if not evaluator:
                logger.warning(
                    "No evaluator registered for assertion type: %s",
                    assertion.assertion_type,
                )
                continue

            evaluated_total += 1
            assertion_issues = evaluator.evaluate(
                assertion=assertion,
                payload=payload,
                context=context,
            )
            issues.extend(assertion_issues)

        total = evaluated_total
        failures = self._count_assertion_failures(issues)
        return AssertionEvaluationResult(
            issues=issues,
            total=total,
            failures=failures,
        )

    def evaluate_assertions_for_stages(
        self,
        *,
        validator: Validator,
        ruleset: Ruleset | None,
        payload: Any,
        stages: tuple[str, ...] = ("input", "output"),
        exclude_assertion_types: set[str] | None = None,
    ) -> AssertionEvaluationResult:
        """Evaluate assertions for multiple stages against one payload.

        Inline validators such as Basic, JSON Schema, and XML Schema do not
        have a separate processor boundary, but authors can still add
        assertions that resolve as either input-stage or output-stage checks.
        This helper keeps the aggregation of issues and assertion counts
        consistent across those validators.
        """
        issues: list[ValidationIssue] = []
        total_assertions = 0
        total_failures = 0

        for stage in stages:
            result = self.evaluate_assertions_for_stage(
                validator=validator,
                ruleset=ruleset,
                payload=payload,
                stage=stage,
                exclude_assertion_types=exclude_assertion_types,
            )
            issues.extend(result.issues)
            total_assertions += result.total
            total_failures += result.failures

        return AssertionEvaluationResult(
            issues=issues,
            total=total_assertions,
            failures=total_failures,
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
