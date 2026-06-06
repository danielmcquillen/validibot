"""
Base class for advanced validators requiring dedicated compute resources.

Advanced validators include container-based validators that dispatch work to
**validator jobs** — self-contained Docker containers defined in the
``validibot-validator-backends`` repository — as well as compute-intensive
validators that call external APIs (e.g., AI-assisted validation via LLM
providers).

For container-based validators, this class handles envelope building, job
dispatch, callback processing, and assertion evaluation; the actual
domain-specific computation (running an EnergyPlus simulation, executing
an FMU) happens inside the container job.

All advanced validators follow the same lifecycle:

1. Validate run_context (must have validation_run and step)
2. Get the configured ExecutionBackend (Docker socket or Cloud Run)
3. **Pre-process the submission** via ``preprocess_submission()`` — e.g.,
   resolve parameterized templates into a concrete model file.  This
   runs before backend dispatch so all platforms see the same content.
4. Build an ExecutionRequest and call backend.execute()
5. For async backends: return pending result (passed=None)
6. For sync backends: process output envelope immediately
7. (On callback) Download and parse the output envelope
8. Extract issues from envelope messages
9. Extract output signals via ``extract_output_signals()``
10. Evaluate output-stage assertions against those signals
11. Return final ValidationResult with signals populated

Subclasses implement ``extract_output_signals()`` to handle their
validator-specific envelope structure and may override
``preprocess_submission()`` for domain-specific input transformation.
The base class handles the shared orchestration, response conversion,
and assertion evaluation.
"""

from __future__ import annotations

import json
import logging
from abc import abstractmethod
from typing import TYPE_CHECKING
from typing import Any

from django.core.exceptions import ValidationError
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
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


class AdvancedValidator(BaseValidator):
    """
    Abstract base for container-based validators using the Template Method pattern.

    Advanced validators dispatch validation work to external container jobs
    (Docker containers defined in the ``validibot-validator-backends`` repo)
    via the ExecutionBackend abstraction. This base class handles the shared
    lifecycle:

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

    @abstractmethod
    def extract_output_signals(self, output_envelope: Any) -> dict[str, Any] | None:
        """
        Extract assertion signals from the validator-specific output envelope.

        Each advanced validator type produces outputs in its own Pydantic envelope
        structure (defined in validibot_shared). This method extracts the signals
        that can be referenced in output-stage assertions.

        Declared as an instance method (not ``@classmethod``) so subclasses
        like EnergyPlus can reach ``self.run_context.step.validator`` to
        scope catalog lookups to the exact validator row bound to this
        step's WorkflowStep. Without that scoping, multi-version catalogs
        (e.g. validator revisions 1 and 2 co-existing during a rollout) can silently
        drop or admit the wrong outputs.

        Args:
            output_envelope: The typed Pydantic envelope from validibot_shared
                (e.g., EnergyPlusOutputEnvelope, FMUOutputEnvelope).

        Returns:
            Dict mapping catalog slugs to values for CEL evaluation, or None if
            no signals can be extracted.
        """
        ...

    # NOTE: ``extract_input_signals(payload)`` lives on ``BaseValidator``
    # with a default of None. Advanced validators that parse an arcane
    # format (EnergyPlus IDF, future IFC, gbXML, etc.) override it to
    # expose parsed facts in the ``i.*`` namespace. Called after
    # ``preprocess_submission()`` so that template-resolved submissions
    # are parsed against the resolved IDF. See ADR-2026-05-22 for
    # design rationale and acceptance criteria.

    # PUBLIC METHODS
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def preprocess_submission(
        self,
        *,
        step: WorkflowStep,
        submission: Submission,
    ) -> dict[str, object]:
        """Pre-process the submission before execution dispatch.

        Called by ``validate()`` after backend availability checks but
        before building the ``ExecutionRequest``.  Subclasses override
        this to transform the submission content — for example, resolving
        parameterized templates into a concrete model file.

        After preprocessing, the submission should look identical to a
        direct upload.  Execution backends never need to know that
        preprocessing occurred.

        The default implementation is a no-op that returns an empty dict.

        Args:
            step: The WorkflowStep containing config and resources.
            submission: The Submission instance.  May be mutated in-place
                (e.g., ``submission.content`` overwritten with resolved
                content).

        Returns:
            Extra metadata dict to merge into the step run stats (e.g.,
            ``template_parameters_used``).  Empty dict when no
            preprocessing was needed.

        Raises:
            ValidationError: If preprocessing discovers invalid input
                (e.g., bad template parameters).  The caller converts
                this into a user-friendly ``ValidationResult``.
            ValueError: If preprocessing fails unexpectedly.
        """
        return {}

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

        # Pre-process the submission (e.g., resolve parameterized templates).
        # This runs before backend dispatch so all platforms see the same
        # resolved content.
        preprocessing_metadata: dict[str, object] = {}
        try:
            preprocessing_metadata = self.preprocess_submission(
                step=step,
                submission=submission,
            )
        except ValidationError as exc:
            messages = exc.messages if hasattr(exc, "messages") else [str(exc)]
            issues = [
                ValidationIssue(
                    path="",
                    message=msg,
                    severity=Severity.ERROR,
                )
                for msg in messages
            ]
            return ValidationResult(passed=False, issues=issues, stats={})

        # ── INPUT-STAGE GATE (per ADR-2026-05-22) ───────────────────
        # Per the ADR's headline promise, advanced validators evaluate
        # input-stage assertions *before* dispatching the container. The
        # parser hook (extract_input_signals) runs against the resolved
        # submission, populates the i.* namespace, and input-stage
        # assertions get a chance to gate dispatch.
        #
        # If any input-stage assertion fails with severity=ERROR, we
        # short-circuit: no container dispatch, run finishes with the
        # failure findings, no compute cost paid. This is the whole
        # point of input-stage assertions for arcane-format validators.
        input_stage_result = self._evaluate_input_stage_and_persist(
            validator=validator,
            ruleset=ruleset,
            submission=submission,
            run=run,
            step=step,
        )
        if input_stage_result is not None and self._has_blocking_failure(
            input_stage_result,
        ):
            input_stage_result.stats = {
                **(input_stage_result.stats or {}),
                **preprocessing_metadata,
                "dispatch_skipped": "input_stage_assertion_failed",
            }
            return input_stage_result

        request = ExecutionRequest(
            run=run,
            validator=validator,
            submission=submission,
            step=step,
        )

        response = backend.execute(request)
        result = self._response_to_result(response, is_async=backend.is_async)

        # Merge preprocessing metadata into result stats so it gets
        # persisted in step_run.output (e.g., template_parameters_used).
        if preprocessing_metadata:
            result.stats = {**(result.stats or {}), **preprocessing_metadata}

        # Prepend any non-blocking input-stage findings so they appear in
        # the result alongside output-stage findings. (Blocking failures
        # already returned above; this branch handles warnings/info.)
        if input_stage_result is not None and input_stage_result.issues:
            result.issues = list(input_stage_result.issues) + list(result.issues or [])

        return result

    def _evaluate_input_stage_and_persist(
        self,
        *,
        validator: Validator,
        ruleset: Ruleset | None,
        submission: Submission,
        run: Any,
        step: Any,
    ) -> ValidationResult | None:
        """Evaluate input-stage assertions and persist i.* to the run summary.

        Per ADR-2026-05-22:
          1. Call ``extract_input_signals()`` against the (resolved)
             submission to produce the contract-keyed step input dict.
          2. Persist that dict to ``run.summary["steps"][step_key]["input"]``
             so downstream steps can read via ``steps.<key>.input.*`` and
             promoted-input injection can find values for ``s.<name>``.
          3. Evaluate input-stage assertions against the resolved
             submission via ``evaluate_assertions_for_stage(stage="input")``.
             The CEL context builder reads ``i.*`` from the parser result
             plus any pre-resolved bindings.

        Returns ``None`` if neither parsing nor input-stage assertion
        evaluation produced anything actionable. Returns a
        ``ValidationResult`` containing the input-stage findings and
        stats when there is at least one input-stage issue or extracted
        signal. The caller decides whether to short-circuit on blocking
        failures.
        """
        from validibot.validations.validators.base.base import ValidationResult

        # Resolve the submission payload for parser extraction. For
        # text-based arcane formats (EnergyPlus IDF) this is the raw
        # string/bytes content; epJSON is also accepted (the parser
        # handles both formats). For binary formats, subclasses can
        # override this method to provide a different payload view.
        payload = self._resolve_input_stage_payload(submission)

        # Run the parser hook. Default returns None; advanced validators
        # that parse arcane formats override extract_input_signals.
        try:
            parsed_inputs = self.extract_input_signals(payload)
        except Exception:
            # Parser failures are reported as findings via the normal
            # error path elsewhere; here we treat them as "no parsed
            # facts available" so assertion evaluation can still run
            # against whatever bindings produced values.
            parsed_inputs = None

        # Compose the i.* dict from BOTH sources per ADR-2026-05-22:
        #
        # 1. ``StepInputBinding`` rows resolved against the submission
        #    payload — explicit author wiring (FMU model inputs, template
        #    variables). Always wins on key collision because the
        #    binding is the author's explicit declaration of where the
        #    value comes from.
        # 2. Parser-extracted facts from ``extract_input_signals()`` —
        #    implicit facts the validator can derive from the payload
        #    (EnergyPlus IDF parser facts). Fills in keys not already
        #    set by bindings.
        #
        # The May 2026 review caught that this method was previously
        # parser-only despite docs claiming both sources populate i.*.
        # FMU/template input-stage assertions saw an empty i.* and any
        # reference like ``i.wall_r_value`` silently resolved to null.
        contract_inputs: dict[str, Any] = {}
        try:
            bound_inputs = self._resolve_bound_input_context(payload)
        except Exception:
            # Binding resolution failures are surfaced as findings via
            # the launcher's normal error path; here we treat them as
            # "no bound values available" so assertion evaluation can
            # still proceed against whatever parser facts exist.
            bound_inputs = {}
        if isinstance(bound_inputs, dict):
            contract_inputs.update(bound_inputs)
        if isinstance(parsed_inputs, dict):
            for key, value in parsed_inputs.items():
                contract_inputs.setdefault(key, value)

        # Expose the resolved contract-keyed inputs on the run context
        # so _build_cel_context() finds them when populating i.*.
        if self.run_context is not None:
            self.run_context.step_input_contract_values = contract_inputs

        # Persist to run.summary so downstream steps see them via
        # steps.<key>.input.* and so promoted-input injection works.
        if contract_inputs:
            self._persist_step_inputs_to_run_summary(
                run=run,
                step=step,
                inputs=contract_inputs,
            )

        # Evaluate input-stage assertions. We ALWAYS return a
        # ValidationResult when assertions were evaluated (even if
        # they all passed and produced no issues) because:
        #
        # 1. The caller needs the AssertionStats to roll up into the
        #    final step result — without this, passing input-stage
        #    assertions disappear from totals and the run report
        #    undercounts what actually ran.
        # 2. The stats keys are canonical: ``assertion_total`` and
        #    ``assertion_failures``, matching what
        #    AdvancedValidationProcessor reads when persisting step
        #    summaries (see services/step_processor/advanced.py:111).
        #
        # Returning None when there are no issues was the original
        # (incorrect) approach — it silently dropped input-stage
        # totals. Per the May 2026 code review's P2 finding.
        #
        # Enrich the payload with namespaced values (i.* contract
        # keys from parser facts + bindings, plus s.* workflow
        # signals) so BASIC assertions targeting i.<name> /
        # s.<name> resolve via the bare contract_key at the top
        # level. CEL evaluation reads from its own namespaced
        # context and ignores this payload, so the enrichment is a
        # no-op for CEL targets.
        enriched_payload = self._enrich_basic_payload(
            payload,
            stage="input",
        )
        assertion_result = self.evaluate_assertions_for_stage(
            validator=validator,
            ruleset=ruleset,
            payload=enriched_payload,
            stage="input",
        )

        if assertion_result.total == 0:
            # No input-stage assertions were evaluated at all (the step
            # has no input-stage assertions defined). Nothing to roll
            # up; the parser facts are still persisted above for
            # cross-step access.
            return None

        # Persist input-stage assertion counts to step_run.output so the
        # async callback path (post_execute_validate) can read them via
        # _get_stored_assertion_stats() and sum with output-stage
        # counts. Without this, async dispatch loses the input-stage
        # totals between validate() and the callback.
        self._persist_input_stage_assertion_counts(
            run=run,
            step=step,
            total=assertion_result.total,
            failures=assertion_result.failures,
        )

        return ValidationResult(
            passed=assertion_result.failures == 0,
            issues=assertion_result.issues,
            assertion_stats=AssertionStats(
                total=assertion_result.total,
                failures=assertion_result.failures,
            ),
            stats={
                "assertion_total": assertion_result.total,
                "assertion_failures": assertion_result.failures,
            },
        )

    def _persist_input_stage_assertion_counts(
        self,
        *,
        run: Any,
        step: Any,
        total: int,
        failures: int,
    ) -> None:
        """Store input-stage assertion counts on the current StepRun.

        The async callback path reads these from ``step_run.output`` via
        ``_get_stored_assertion_stats()`` so it can sum input-stage and
        output-stage counts into the final assertion totals. Without
        this, input-stage failures/totals would disappear once the
        container dispatch happens.

        Best-effort: if the StepRun for this run+step doesn't exist
        yet (rare — should be created by the orchestrator before
        validate() runs), this silently no-ops rather than blocking
        the validation pipeline.
        """
        try:
            step_run = run.step_runs.filter(workflow_step=step).first()
        except Exception:
            step_run = None
        if step_run is None:
            return
        output = step_run.output or {}
        output["assertion_total"] = total
        output["assertion_failures"] = failures
        step_run.output = output
        step_run.save(update_fields=["output"])

    def _resolve_input_stage_payload(self, submission: Submission) -> Any:
        """Return the payload to pass to extract_input_signals().

        Uses ``submission.get_content()`` to read the payload from
        either the inline ``content`` field OR the ``input_file``
        FileField — this is critical because file-uploaded submissions
        store the IDF/payload in ``input_file``, not ``content``.
        Reading ``submission.content`` directly would return empty
        string for file-backed submissions and the parser would
        produce wrong facts (e.g. zone_count=0 for an IDF that
        actually has zones), causing the input-stage gate to fail or
        pass incorrectly.

        The rest of the execution path uses ``get_content()`` for the
        same reason — this method matches that contract.
        """
        try:
            return submission.get_content()
        except Exception:
            # Defensive: if content retrieval fails (e.g. purged
            # content, missing file), return None and let the parser's
            # type-handling treat it as "no payload available".
            return None

    def _has_blocking_failure(self, result: ValidationResult) -> bool:
        """Whether a ValidationResult contains at least one ERROR finding.

        Used to decide whether to skip container dispatch based on
        input-stage assertion results.
        """
        return any(issue.severity == Severity.ERROR for issue in (result.issues or []))

    def _persist_step_inputs_to_run_summary(
        self,
        *,
        run: Any,
        step: Any,
        inputs: dict[str, Any],
    ) -> None:
        """Store the contract-keyed i.* dict under run.summary["steps"][key]["input"].

        Mirrors the existing output persistence (``step_data["output"]``)
        so cross-step access via ``steps.<key>.input.*`` works
        symmetrically with ``steps.<key>.output.*``. Required by
        ADR-2026-05-22b's symmetric promotion.
        """
        summary = run.summary or {}
        steps = summary.setdefault("steps", {})
        step_key = step.step_key or str(step.id)
        step_data = steps.setdefault(step_key, {})
        step_data["input"] = inputs
        run.summary = summary
        run.save(update_fields=["summary"])

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
        self.run_context = run_context

        issues = self._extract_issues_from_envelope(output_envelope)

        # Extract signals from envelope for downstream steps and assertions
        signals = self.extract_output_signals(output_envelope) or {}
        logger.debug(
            "post_execute_validate: extracted %d signals: %s",
            len(signals),
            list(signals.keys()),
        )

        # Evaluate output-stage assertions if we have context
        assertion_total = 0
        assertion_failures = 0
        if run_context and run_context.step:
            validator = run_context.step.validator
            ruleset = run_context.step.ruleset
            logger.debug(
                "post_execute_validate: validator=%s ruleset=%s",
                validator,
                ruleset,
            )

            if validator and ruleset:
                # Build assertion payload: merge submission input data with
                # output signals so output-stage assertions can reference
                # both. For example, an FMU assertion like
                # ``Q_cooling_actual < Q_cooling_max * 0.85`` compares an
                # output signal against a user-provided input value.
                #
                # On name collision, the input keeps the bare name and the
                # output is available via the ``output.`` prefix — matching
                # the convention in ``_build_cel_context``.
                #
                # Prefer resolved_inputs (with defaults and nested-path
                # resolution applied) over raw submission JSON when
                # available.
                resolved_inputs = self._get_resolved_inputs(run_context)
                assertion_payload = self._build_assertion_payload(
                    signals,
                    run_context,
                    resolved_inputs=resolved_inputs,
                )
                # Additionally enrich with workflow signals so BASIC
                # assertions targeting s.<name> resolve via the bare
                # name. ``_build_assertion_payload`` already merges
                # resolved_inputs and signals; this adds workflow_signals
                # for BASIC compatibility (CEL reads them from its own
                # context regardless). Output stage so the helper skips
                # re-running StepInputBinding resolution.
                assertion_payload = self._enrich_basic_payload(
                    assertion_payload,
                    stage="output",
                    output_signals=None,
                )

                assertion_result = self.evaluate_assertions_for_stage(
                    validator=validator,
                    ruleset=ruleset,
                    payload=assertion_payload,
                    stage="output",
                )
                logger.debug(
                    "post_execute_validate: assertions evaluated — "
                    "total=%d failures=%d issues=%d",
                    assertion_result.total,
                    assertion_result.failures,
                    len(assertion_result.issues),
                )
                issues.extend(assertion_result.issues)
                assertion_total = assertion_result.total
                assertion_failures = assertion_result.failures
            else:
                logger.debug(
                    "post_execute_validate: skipping assertions — "
                    "validator=%s ruleset=%s",
                    validator,
                    ruleset,
                )

        # Determine pass/fail based on envelope status and assertion failures.
        passed = self._determine_passed(
            output_envelope,
            assertion_failures=assertion_failures,
        )

        # Build stats with outputs if available
        stats = self._extract_outputs_stats(output_envelope)

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

    @staticmethod
    def _get_resolved_inputs(run_context: RunContext | None) -> dict[str, Any] | None:
        """Retrieve resolved input values from the step_run, if available.

        The envelope builder stores fully-resolved input values (with
        defaults applied and nested paths resolved) on
        ``step_run.output["resolved_inputs"]`` during FMU envelope
        construction.  This method looks up the active step_run for the
        current run/step pair and returns those values.

        Returns ``None`` when no step_run exists or no resolved_inputs
        were stored (e.g., legacy steps without StepInputBinding rows),
        allowing the caller to fall back to raw submission JSON.
        """
        if not run_context or not run_context.validation_run or not run_context.step:
            return None

        from validibot.validations.models import ValidationStepRun

        try:
            step_run = (
                ValidationStepRun.objects.filter(
                    validation_run=run_context.validation_run,
                    workflow_step=run_context.step,
                )
                .order_by("-created")
                .first()
            )
        except Exception:
            logger.debug(
                "Could not look up step_run for resolved_inputs (run=%s, step=%s)",
                getattr(run_context.validation_run, "id", "?"),
                getattr(run_context.step, "id", "?"),
            )
            return None

        if step_run and step_run.output:
            return step_run.output.get("resolved_inputs")
        return None

    @staticmethod
    def _build_assertion_payload(
        signals: dict[str, Any],
        run_context: RunContext | None,
        resolved_inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Build a combined payload for output-stage assertion evaluation.

        Output-stage assertions often compare validator outputs against user
        inputs (e.g., ``Q_cooling_actual <= Q_cooling_max``).  The output
        signals alone don't contain the input values, so we merge in the
        resolved input values.

        When ``resolved_inputs`` is provided (from ``step_run.output``),
        those values are used as the input side of the payload.  This
        ensures assertions see the same values — with defaults applied
        and nested paths resolved — that the validator launch used.  When
        not provided, the method falls back to parsing the raw submission
        JSON for backward compatibility.

        **Name collision convention** (matches ``_build_cel_context``):

        - Input values keep their **bare name** (``Q_cooling_max``).
        - Output values keep their bare name when there is no collision.
        - All output values are also available under a nested ``output``
          dict so ``output.T_room`` resolves correctly via both CEL
          member access and basic-assertion dot-path navigation.
        - On collision, the bare name keeps the input value and the
          output is only reachable via ``output.<name>``.

        **Resulting structure example**::

            {
                "Q_cooling_max": 6000,          # input (bare)
                "T_room": 296.63,               # output (bare, no collision)
                "Q_cooling_actual": 5172.83,    # output (bare, no collision)
                "output": {                     # nested namespace
                    "T_room": 296.63,
                    "Q_cooling_actual": 5172.83,
                },
            }

        This is a no-op when neither resolved_inputs nor parseable JSON
        submission content is available (e.g., EnergyPlus IDF files) —
        only the output signals are returned.
        """
        # When resolved_inputs are available, use them directly instead
        # of parsing the raw submission JSON.  This is the preferred path
        # for FMU/generic validators with StepInputBinding rows.
        if resolved_inputs:
            merged = dict(resolved_inputs)
            output_ns: dict[str, object] = {}
            for key, value in signals.items():
                if key not in merged:
                    merged[key] = value
                output_ns[key] = value
            if output_ns:
                merged["output"] = output_ns
            return merged

        # Fallback: parse raw submission JSON (for legacy steps without
        # StepInputBinding rows or when resolved_inputs aren't available).
        if not run_context or not run_context.validation_run:
            return dict(signals)

        submission = getattr(run_context.validation_run, "submission", None)
        if not submission:
            return dict(signals)

        try:
            content = submission.get_content()
            if not content:
                return dict(signals)
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                # Start with submission inputs (bare names).
                merged = dict(parsed)
                # Build a nested ``output`` namespace so that
                # ``output.T_room`` works in both CEL (member access)
                # and basic assertions (dot-path navigation).  All
                # output signals are always available under the
                # namespace; on collision the input keeps the bare
                # name and the output is only reachable via
                # ``output.<name>``.
                output_ns = {}
                for key, value in signals.items():
                    if key not in merged:
                        # No collision — bare name resolves to output.
                        merged[key] = value
                    output_ns[key] = value
                if output_ns:
                    merged["output"] = output_ns
                return merged
        except (json.JSONDecodeError, TypeError, Exception):
            logger.debug(
                "Could not parse submission content as JSON for assertion "
                "payload merging (run=%s); using output signals only.",
                getattr(run_context.validation_run, "id", "?"),
            )

        return dict(signals)

    @staticmethod
    def _extract_issues_from_envelope(envelope: Any) -> list[ValidationIssue]:
        """Convert envelope messages to ValidationIssue objects."""
        from validibot_shared.validations.envelopes import Severity as EnvelopeSeverity

        severity_map = {
            EnvelopeSeverity.ERROR: Severity.ERROR,
            EnvelopeSeverity.WARNING: Severity.WARNING,
            EnvelopeSeverity.INFO: Severity.INFO,
        }
        return [
            ValidationIssue(
                path=msg.location.path if msg.location else "",
                message=msg.text,
                severity=severity_map.get(msg.severity, Severity.INFO),
            )
            for msg in envelope.messages
        ]

    @staticmethod
    def _determine_passed(envelope: Any, *, assertion_failures: int = 0) -> bool:
        """Determine pass/fail from envelope status and assertion failures."""
        from validibot_shared.validations.envelopes import ValidationStatus

        if envelope.status == ValidationStatus.SUCCESS:
            return assertion_failures == 0
        return False

    @staticmethod
    def _extract_outputs_stats(envelope: Any) -> dict[str, Any]:
        """Extract outputs from envelope into a stats dict."""
        if not envelope.outputs:
            return {}
        if hasattr(envelope.outputs, "model_dump"):
            return {"outputs": envelope.outputs.model_dump(mode="json")}
        if isinstance(envelope.outputs, dict):
            return {"outputs": envelope.outputs}
        return {}

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

        # Trust ADR Phase 5 Session A — propagate the resolved
        # validator backend image digest (sync backends only). Async
        # backends persist this directly on the step run at launch
        # time, so they leave ``response.validator_backend_image_digest``
        # as ``None`` and the step processor reads from the column
        # later. Carrying it in ``stats`` lets the step processor
        # write the typed column without needing an extra parameter
        # on the validate() return path.
        backend_image_digest = getattr(
            response,
            "validator_backend_image_digest",
            None,
        )
        if backend_image_digest:
            stats["validator_backend_image_digest"] = backend_image_digest

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

        # Defer output findings to post_execute_validate(). The step processor
        # calls that method for both synchronous and callback completions, and
        # persisting envelope messages here as well would duplicate every
        # container finding. SHACL also needs that single post-processing pass
        # to preserve its richer outputs.findings metadata.
        stats.update(self._extract_outputs_stats(response.output_envelope))
        result = ValidationResult(
            passed=self._determine_passed(response.output_envelope),
            issues=[],
            stats=stats,
        )
        # Attach envelope for processor to use in post_execute_validate()
        result.output_envelope = response.output_envelope
        return result
