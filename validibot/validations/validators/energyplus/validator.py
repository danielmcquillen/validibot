"""
EnergyPlus validator.

This validator forwards incoming EnergyPlus submissions (epJSON or IDF) to
container-based validator jobs via the ExecutionBackend abstraction. This works
across different deployment targets:

- Docker Compose: Docker containers via local socket (synchronous)
- GCP: Cloud Run Jobs (async with callbacks)
- AWS: AWS Batch (future)

## Preprocessing

When a workflow step uses a parameterized IDF template, the submitter uploads
a JSON dict of variable values instead of a complete IDF.  The
``preprocess_submission()`` override delegates to ``energyplus.preprocessing``
to resolve the template into a full IDF **before** backend dispatch.  After
preprocessing, the submission looks identical to a direct-IDF upload —
execution backends never need to know templates exist.

## Output Envelope Structure

The EnergyPlus validator container produces an ``EnergyPlusOutputEnvelope``
(from validibot_shared.energyplus.envelopes) containing:

- outputs.metrics: EnergyPlusSimulationMetrics with fields like:
  - site_eui_kwh_m2: Site energy use intensity
  - site_electricity_kwh: Total electricity consumption
  - site_natural_gas_kwh: Total gas consumption
  - etc. (see validibot_shared/energyplus/models.py)

These metrics are extracted via ``extract_output_signals()`` for use in
output-stage assertions (e.g., "site_eui_kwh_m2 < 100").
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any

from validibot.validations.constants import Severity
from validibot.validations.validators.base.advanced import AdvancedValidator

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.submissions.models import Submission
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Validator
    from validibot.validations.validators.base.base import ValidationResult
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


class EnergyPlusValidator(AdvancedValidator):
    """
    EnergyPlus simulation validator.

    Dispatches EnergyPlus submissions (IDF/epJSON) to container-based validator
    jobs via the ExecutionBackend. The actual simulation runs inside a Docker
    container defined in the ``validibot-validator-backends`` repository.

    Overrides:

    - ``preprocess_submission()`` — resolves parameterized IDF templates into
      a complete IDF before backend dispatch (delegates to
      ``energyplus.preprocessing``).
    - ``extract_output_signals()`` — extracts simulation metrics for assertions.

    The shared validate/post_execute_validate lifecycle is handled by
    ``AdvancedValidator``.
    """

    @property
    def validator_display_name(self) -> str:
        return "EnergyPlus"

    def _should_filter_warnings(self, run_context: RunContext | None) -> bool:
        """Check whether simulation warnings should be suppressed.

        Reads ``show_energyplus_warnings`` from the step's ``display_settings``.
        It is cosmetic (it filters which non-blocking warnings are *shown*, never
        pass/fail), so it lives in the display bucket (ADR-2026-06-18). When
        False, non-ERROR issues from the EnergyPlus ``.err`` file should be
        stripped before they are persisted as findings.
        """
        step = run_context.step if run_context else None
        if not step:
            return False
        display_settings = step.display_settings or {}
        return not display_settings.get("show_energyplus_warnings", True)

    def _filter_issues(
        self,
        result: ValidationResult,
        run_context: RunContext | None,
    ) -> ValidationResult:
        """Remove non-ERROR issues from result when warnings are suppressed."""
        if self._should_filter_warnings(run_context):
            result.issues = [
                issue for issue in result.issues if issue.severity == Severity.ERROR
            ]
        return result

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset | None,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """Dispatch to container, filtering warnings from sync results.

        For sync backends (Docker Compose), ``validate()`` returns with
        envelope messages already extracted into ``result.issues``. These
        issues are persisted by the processor *before* ``post_execute_validate()``
        runs.  We must filter here to prevent warnings from being saved as
        findings when ``show_energyplus_warnings`` is disabled.
        """
        result = super().validate(validator, submission, ruleset, run_context)
        return self._filter_issues(result, run_context)

    def preprocess_submission(
        self,
        *,
        step: WorkflowStep,
        submission: Submission,
    ) -> dict[str, object]:
        """Resolve parameterized IDF templates before execution dispatch.

        If the step has a ``MODEL_TEMPLATE`` resource, the submission is
        treated as a JSON dict of variable values.  This method delegates
        to ``energyplus.preprocessing`` which merges values with author
        defaults, validates constraints, substitutes ``$VARIABLE``
        placeholders, and overwrites ``submission.content`` with the
        resolved IDF.

        For direct-IDF submissions (no template resource), this is a no-op.

        Returns:
            Metadata dict with ``template_parameters_used`` and
            ``template_warnings`` (merged into ``step_run.output`` by the
            base class), or empty dict for direct-mode submissions.
        """
        from validibot.validations.validators.energyplus.preprocessing import (
            preprocess_energyplus_submission,
        )

        result = preprocess_energyplus_submission(step=step, submission=submission)
        return result.template_metadata

    def post_execute_validate(
        self,
        output_envelope: Any,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """Process container output, optionally filtering simulation warnings.

        Delegates to the base ``AdvancedValidator.post_execute_validate()``
        for envelope processing, signal extraction, and assertion evaluation.
        Then applies warning filtering if ``show_energyplus_warnings`` is
        disabled in the step config.

        This method handles the async callback path and the output-stage
        extraction.  The sync path's initial extraction is filtered in
        ``validate()`` above.
        """
        result = super().post_execute_validate(output_envelope, run_context)
        return self._filter_issues(result, run_context)

    def extract_output_signals(self, output_envelope: Any) -> dict[str, Any] | None:
        """
        Extract simulation metrics from an EnergyPlus output envelope.

        EnergyPlus envelopes (EnergyPlusOutputEnvelope from validibot_shared)
        store metrics in outputs.metrics as an EnergyPlusSimulationMetrics
        Pydantic model. Fields include site_eui_kwh_m2, site_electricity_kwh,
        etc.

        Per ADR-2026-05-22 and the May 2026 code review's P2 finding:
        the catalog is the authoritative contract for which signals
        belong in ``o.*``. The Pydantic model may carry additional
        fields (left over from older shared-package versions or
        future fields added before the catalog catches up) — we
        filter to the catalog's declared OUTPUT-direction
        ``contract_key`` values so a shared-package version mismatch
        can't silently leak fields like ``zone_count`` back into
        ``o.*`` after the catalog removed them.

        This is an instance method (not a classmethod) so it can reach
        ``self.run_context.step.validator`` for catalog scoping. The
        May 2026 review showed that the old classmethod implementation
        used ``Validator.objects.filter(...).first()`` which can pick
        the wrong row when EnergyPlus revisions 1 and 2 co-exist in
        the database — silently dropping legitimate newer outputs or
        admitting retired older ones. By scoping the catalog
        lookup to the validator bound to this step's run, we always
        use the catalog version that produced this run's contract.

        Args:
            output_envelope: EnergyPlusOutputEnvelope from the validator container.

        Returns:
            Dict of metrics keyed by field name (matching catalog slugs), with
            None values filtered out AND filtered to keys the catalog
            declares as OUTPUT-direction signals. Returns None if
            extraction fails.
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
                raw_metrics = metrics.model_dump(mode="json")
            elif isinstance(metrics, dict):
                raw_metrics = metrics
            else:
                return None

            non_null = {k: v for k, v in raw_metrics.items() if v is not None}
            return self._filter_to_catalog_outputs(non_null)
        except (AttributeError, TypeError, ValueError) as exc:
            logger.warning(
                "Could not extract signals from EnergyPlus envelope: %s",
                exc,
                exc_info=True,
            )

        return None

    def _filter_to_catalog_outputs(
        self,
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        """Restrict raw metric dict to keys declared as OUTPUT in the catalog.

        Per ADR-2026-05-22: the catalog is the public contract for
        which signals live in ``o.*``. Anything in the shared
        Pydantic envelope that the catalog doesn't declare as an
        OUTPUT-direction entry must not appear in ``o.*`` — otherwise
        a shared-package version drift (e.g., the shared package
        ships a ``zone_count`` field that the catalog removed) would
        silently leak the field into authors' CEL contexts.

        Catalog scoping order (most specific first):

        1. ``self.run_context.step.validator`` — the exact validator
           row bound to this step's WorkflowStep. This is the only
           lookup that is guaranteed correct when multiple catalog
           versions (e.g. v1 and v2) co-exist in the database,
           because the FK on WorkflowStep points at the specific row
           that produced this step's contract.
        2. Newest system EnergyPlus validator by ``-version`` —
           defensive fallback for the rare case where extraction is
           called without a run context (tests, sync_validators). The
           ordering picks the highest integer version, matching what
           ``sync_validators`` writes as the current catalog.

        Returns the input dict unchanged if catalog lookup fails for
        any reason (defensive: prefer wrong-but-old behaviour over
        breaking output extraction entirely). The catalog reference
        for EnergyPlus is centralised in ``config.py`` and synced via
        ``sync_validators``.
        """
        try:
            from validibot.validations.constants import SignalDirection
            from validibot.validations.constants import ValidationType
            from validibot.validations.models import StepIODefinition
            from validibot.validations.models import Validator

            validator = self._resolve_catalog_validator()
            if validator is None:
                # Last-resort fallback: pick the newest system EnergyPlus
                # validator. Sorting by ``-version`` matches the catalog
                # writer's notion of "current". If even this lookup
                # fails, fall through to the unfiltered dict — better
                # than dropping all outputs.
                validator = (
                    Validator.objects.filter(
                        validation_type=ValidationType.ENERGYPLUS,
                        is_system=True,
                    )
                    .order_by("-version")
                    .first()
                )
            if validator is None:
                return metrics
            allowed_keys = set(
                StepIODefinition.objects.filter(
                    validator=validator,
                    direction=SignalDirection.OUTPUT,
                ).values_list("contract_key", flat=True)
            )
            if not allowed_keys:
                # No catalog entries means we can't filter; preserve
                # existing behaviour rather than producing an empty dict.
                return metrics
            return {k: v for k, v in metrics.items() if k in allowed_keys}
        except Exception:
            # Defensive: any DB/import error falls back to the raw
            # dict so output extraction still works in degraded
            # environments (e.g., when called during sync_validators
            # before the validator row exists).
            return metrics

    def _resolve_catalog_validator(self):
        """Return the Validator row bound to the current step, if known.

        Catalog filtering needs the exact validator that produced this
        step's contract — not "any system EnergyPlus validator". When
        multiple catalog versions co-exist in the database, only the
        step's FK reliably identifies the right one.

        Returns None when there is no run context (e.g., the method is
        being exercised by a unit test that constructed the validator
        directly), letting the caller fall back to a global lookup.
        """
        run_context = getattr(self, "run_context", None)
        if run_context is None:
            return None
        step = getattr(run_context, "step", None)
        if step is None:
            return None
        return getattr(step, "validator", None)

    def extract_input_signals(self, payload: Any) -> dict[str, Any] | None:
        """
        Parse the (resolved) IDF or epJSON and extract declared step inputs.

        Per ADR-2026-05-22, this returns the three POC step inputs:
            - ``idf_version`` (string)
            - ``zone_count`` (int)
            - ``north_axis_deg`` (number)

        Phase 2 will extend this to ~12 entries
        (building_name, terrain, solar_distribution, timestep_per_hour,
        surface_count, window_count, construction_count, run_period_count,
        has_hvac, ...).

        Called by ``_build_cel_context()`` at input stage. Failures during
        extraction are logged but do not abort assertion evaluation — the
        ``i.*`` namespace simply omits values that could not be parsed,
        and the catalog's ``on_missing`` policy determines whether that
        absence is acceptable.

        Args:
            payload: The submission payload as it would reach the container.
                For EnergyPlus this is either raw IDF text (string or bytes,
                for direct-IDF and template-resolved submissions) or a
                parsed epJSON dict (for epJSON submissions).

        Returns:
            Dict mapping catalog ``contract_key`` to extracted values, or
            None if the payload is not recognisable as IDF or epJSON.
        """
        from validibot.validations.validators.energyplus import idf_facts

        try:
            return idf_facts.extract_poc_facts(payload)
        except Exception as exc:
            logger.warning(
                "Could not extract step input facts from IDF/epJSON: %s",
                exc,
                exc_info=True,
            )
            return None
