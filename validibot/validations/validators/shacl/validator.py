"""SHACL validator — dispatches to the isolated container backend.

SHACL parses untrusted RDF and executes author-supplied SPARQL (SHACL-AF
constraints and SPARQL-ASK assertions). That work must never run next to the
worker's database credentials, identity, or network, so — like EnergyPlus and
FMU — SHACL is an :class:`AdvancedValidator`: Django resolves shapes/settings/
assertions from the database, ships them in a ``SHACLInputEnvelope``, and the
``validibot-validator-backends`` container does all graph/SPARQL execution. See
ADR-2026-05-18 for the engine design and the cross-repo plan for the isolation
rationale.

The base class handles the full lifecycle (input-stage gate, dispatch via the
configured execution backend, sync/async completion). This subclass supplies two
things:

1. :meth:`extract_output_signals` — the ``o.*`` signal dict for CEL/Basic
   assertions, pulled from the container's ``SHACLOutputs``.
2. :meth:`post_execute_validate` — a SHACL-specific override that (a) rebuilds
   findings from the structured ``outputs.findings`` so the SHACL ``meta``
   (focus node, source shape, constraint component) and SPARQL-ASK
   ``assertion_id`` survive, (b) evaluates the Django-side CEL/Basic output
   assertions while **excluding** SHACL-type rows (those run as SPARQL in the
   container), and (c) folds the container's SPARQL-ASK assertion tallies into
   the final :class:`AssertionStats`.

Library-level custom SHACL validators (org-owned ``Validator`` rows with a
populated ``default_ruleset``) reuse this same class and ``validation_type``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any

from validibot.validations.constants import AssertionType
from validibot.validations.constants import Severity
from validibot.validations.validators.base.advanced import AdvancedValidator
from validibot.validations.validators.base.base import AssertionStats
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.base.base import ValidationResult

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext

logger = logging.getLogger(__name__)

# Map the container's finding-severity strings back to the Django Severity enum.
_SEVERITY_FROM_STRING = {
    "ERROR": Severity.ERROR,
    "WARNING": Severity.WARNING,
    "INFO": Severity.INFO,
    "SUCCESS": Severity.SUCCESS,
}

# The o.* signal keys this validator exposes — must match the catalog entries in
# config.py (the "catalog is the contract" rule). Extra fields on SHACLOutputs
# (report turtle, hashes, assertion tallies) are surfaced via stats, not signals.
_SIGNAL_KEYS = (
    "parse_ok",
    "parse_serialization",
    "triple_count",
    "namespaces_present",
    "has_s223_namespace",
    "has_g36_namespace",
    "has_brick_namespace",
    "shacl_violation_count",
    "shacl_warning_count",
    "shacl_info_count",
    "shacl_total_count",
)


class SHACLValidator(AdvancedValidator):
    """SHACL RDF-graph validator dispatched to an isolated container backend."""

    @property
    def validator_display_name(self) -> str:
        return "SHACL"

    def extract_output_signals(self, output_envelope: Any) -> dict[str, Any] | None:
        """Pull the ``o.*`` signal dict from the container's ``SHACLOutputs``.

        Filtered to the catalog-declared keys so any extra output fields cannot
        leak into the ``o.*`` namespace — the same "catalog is the contract"
        invariant EnergyPlus/FMU enforce on their extractors.
        """
        outputs = getattr(output_envelope, "outputs", None)
        if outputs is None:
            return None
        return {key: getattr(outputs, key, None) for key in _SIGNAL_KEYS}

    def post_execute_validate(
        self,
        output_envelope: Any,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """Process the container output: findings, signals, and folded assertions.

        Overrides the base because SHACL needs three things the generic path
        doesn't provide:

        1. **Rich findings.** The generic ``_extract_issues_from_envelope`` reads
           the lossy ``messages`` list. We instead rebuild ``ValidationIssue``
           rows from ``outputs.findings`` so SHACL ``meta`` and SPARQL-ASK
           ``assertion_id`` are preserved for display and attribution.
        2. **Excluded SHACL assertions.** SPARQL-ASK assertions already ran in the
           container; the Django-side CEL/Basic pass must exclude
           ``AssertionType.SHACL`` so they aren't double-counted or re-run
           (against a graph Django no longer has).
        3. **Folded assertion totals.** Final assertion counts = container
           SPARQL-ASK tallies + Django CEL/Basic tallies.
        """
        self.run_context = run_context

        outputs = getattr(output_envelope, "outputs", None)
        issues = self._issues_from_outputs(outputs)
        signals = self.extract_output_signals(output_envelope) or {}

        # Container-side SPARQL-ASK assertion tallies.
        container_total = getattr(outputs, "assertion_total", 0) if outputs else 0
        container_failures = getattr(outputs, "assertion_failures", 0) if outputs else 0

        cel_total = 0
        cel_failures = 0
        if run_context and run_context.step:
            validator = run_context.step.validator
            ruleset = run_context.step.ruleset
            if validator and ruleset:
                resolved_inputs = self._get_resolved_inputs(run_context)
                payload = self._build_assertion_payload(
                    signals,
                    run_context,
                    resolved_inputs=resolved_inputs,
                )
                payload = self._enrich_basic_payload(
                    payload,
                    stage="output",
                    output_signals=None,
                )
                # Exclude SHACL-type rows — those executed in the container as
                # SPARQL ASKs and are already counted in container_* above.
                assertion_result = self.evaluate_assertions_for_stage(
                    validator=validator,
                    ruleset=ruleset,
                    payload=payload,
                    stage="output",
                    exclude_assertion_types={AssertionType.SHACL},
                )
                issues.extend(assertion_result.issues)
                cel_total = assertion_result.total
                cel_failures = assertion_result.failures

        assertion_total = container_total + cel_total
        assertion_failures = container_failures + cel_failures

        passed = self._determine_passed(
            output_envelope,
            assertion_failures=assertion_failures,
        )

        stats = self._build_stats(outputs)

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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _issues_from_outputs(outputs: Any) -> list[ValidationIssue]:
        """Rebuild ValidationIssue rows from the container's structured findings."""
        findings = getattr(outputs, "findings", None) or []
        issues: list[ValidationIssue] = []
        for f in findings:
            issues.append(
                ValidationIssue(
                    path=getattr(f, "path", "") or "",
                    message=f.message,
                    severity=_SEVERITY_FROM_STRING.get(f.severity, Severity.ERROR),
                    code=getattr(f, "code", "") or "",
                    meta=dict(getattr(f, "meta", None) or {}) or None,
                    assertion_id=getattr(f, "assertion_id", None),
                ),
            )
        return issues

    @staticmethod
    def _build_stats(outputs: Any) -> dict[str, Any]:
        """Surface SHACL run metadata + the serialized report for evidence/UI.

        Preserves the top-level stats keys the in-process validator produced
        (``results_graph_turtle`` for download, the shape/ontology hashes, the
        result-handling mode) so downstream consumers (evidence manifest,
        report-download view) keep working unchanged. The full envelope is also
        serialized into step output by the processor.
        """
        if outputs is None:
            return {}
        return {
            "parse_serialization": getattr(outputs, "parse_serialization", ""),
            "triple_count": getattr(outputs, "triple_count", 0),
            "shacl_total_count": getattr(outputs, "shacl_total_count", 0),
            "shacl_shapes_sha256": getattr(outputs, "shacl_shapes_sha256", ""),
            "shacl_ontology_sha256": getattr(outputs, "shacl_ontology_sha256", ""),
            "advanced_shacl_requested": getattr(
                outputs,
                "advanced_shacl_requested",
                False,
            ),
            "shacl_result_handling": getattr(outputs, "shacl_result_handling", ""),
            "results_graph_turtle": getattr(outputs, "results_graph_turtle", ""),
        }
