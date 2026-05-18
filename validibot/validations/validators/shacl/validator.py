"""SHACL validator orchestration.

The :class:`SHACLValidator` class is intentionally thin — it walks
through the pure functions in :mod:`engine` in order and assembles a
:class:`ValidationResult`. The actual RDF parsing, inference, SHACL
execution, finding mapping, and signal extraction all live in
``engine.py`` so they can be unit-tested in isolation without any
Django dependencies.

See ADR-2026-05-18 ``SHACL Validator for RDF Graph Validation`` for the
end-to-end design, including the library-level custom SHACL validator
path (``validator.default_ruleset`` carries the bundled shapes; the
step-level ``ruleset`` adds project-specific extras).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any

from django.utils.translation import gettext as _

from validibot.validations.constants import Severity
from validibot.validations.validators.base.base import BaseValidator
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.base.base import ValidationResult
from validibot.validations.validators.shacl import engine

if TYPE_CHECKING:
    from validibot.actions.protocols import RunContext
    from validibot.validations.models import Ruleset
    from validibot.validations.models import Submission
    from validibot.validations.models import Validator

logger = logging.getLogger(__name__)


class SHACLValidator(BaseValidator):
    """Generic SHACL validator for RDF graphs.

    Validates RDF documents (Turtle, JSON-LD, RDF/XML, N-Triples,
    N-Quads) against SHACL shape collections. The shapes come from two
    sources, merged at validation time:

    1. ``validator.default_ruleset`` — for library-level custom SHACL
       validators that an organisation has created (e.g.
       ``MeridianCx 223P + G36 Validator``). The default_ruleset bundles
       the standard shapes once so multiple workflows can reuse them
       without re-uploading.
    2. ``ruleset`` (the step-level ruleset) — project-specific shapes
       layered on top.

    The merge mirrors the assertion-merge pattern in
    :meth:`BaseValidator.evaluate_assertions_for_stage`.

    The engine is pure Python (pyshacl + rdflib + owlrl) and runs
    in-process. It is NOT an advanced (Docker) validator — see
    ADR-2026-05-18 for the cost-benefit analysis.

    Output:

    - ``issues``: structured findings, one per SHACL constraint
      violation. Severity is mapped from ``sh:resultSeverity``
      (Violation → ERROR, Warning → WARNING, Info → INFO). SHACL detail
      (focus node, source shape, constraint component, offending value)
      lives in ``issue.meta``.
    - ``signals``: the ``o.*`` signal dict for CEL assertions
      (``o.shacl_violation_count``, ``o.has_s223_namespace``, etc.).
    - ``stats.results_graph_turtle``: the native SHACL
      ``sh:ValidationReport`` graph serialised as Turtle, available for
      download and re-ingestion by downstream tools (BuildingMOTIF,
      analytics platforms, AI agents).

    The ``passed`` flag is True iff no ``Severity.ERROR`` issues exist.
    Warnings and infos do not block — operators decide whether to gate
    on them via CEL assertions.
    """

    def validate(
        self,
        validator: Validator,
        submission: Submission,
        ruleset: Ruleset,
        run_context: RunContext | None = None,
    ) -> ValidationResult:
        """Validate an RDF submission against the merged SHACL shapes.

        High-level flow (each step delegates to :mod:`engine`):

        1. Read default_ruleset (library validator) and step ruleset.
        2. Merge their shapes_text, ontology_text, and bundled_standards.
        3. Load any opted-in bundled standards (Brick / QUDT). Phase 1
           emits a WARNING when bundles are requested because the
           content ships in Phase 2.
        4. Parse the submission as RDF using the resolved serialization.
        5. Run pyshacl with the resolved inference mode + advanced flag.
        6. Map ``sh:ValidationResult`` nodes to ``ValidationIssue`` rows.
        7. Extract output signals for CEL.
        8. Return ``ValidationResult(passed, issues, signals, stats)``.

        The ``run_context`` argument is accepted for protocol consistency
        but the simple in-process path does not need it.
        """
        self.run_context = run_context

        settings = self._resolve_settings(validator, ruleset)

        # Combine library + step shapes/ontologies before loading bundles.
        merged_shapes, merged_ontology, bundled_standards = (
            engine.merge_shapes_and_ontologies(
                default_shapes_text=settings["default_shapes_text"],
                default_ontology_text=settings["default_ontology_text"],
                default_bundled_standards=settings["default_bundled_standards"],
                step_shapes_text=settings["step_shapes_text"],
                step_ontology_text=settings["step_ontology_text"],
                step_bundled_standards=settings["step_bundled_standards"],
            )
        )

        # Bundled-standards loader is a Phase 1 stub that produces
        # WARNING issues when the operator opted into Brick or QUDT.
        bundled_shapes, bundled_ontology, bundle_warnings = (
            engine.load_bundled_standards(bundled_standards)
        )
        if bundled_shapes:
            merged_shapes = merged_shapes + engine.FILE_SEPARATOR + bundled_shapes
        if bundled_ontology:
            merged_ontology = merged_ontology + engine.FILE_SEPARATOR + bundled_ontology

        # Parse the submission.
        serialization = engine.detect_serialization(
            file_name=getattr(submission, "file_name", None),
            file_type=getattr(submission, "file_type", None),
            explicit_format=settings["submission_format"],
        )
        content = submission.get_content()
        data_graph, parse_error = engine.parse_rdf(content, serialization)
        if data_graph is None:
            return ValidationResult(
                passed=False,
                issues=[
                    *bundle_warnings,
                    ValidationIssue(
                        path="",
                        message=parse_error or _("Failed to parse submission."),
                        severity=Severity.ERROR,
                        code="shacl.parse_failed",
                    ),
                ],
                signals=engine.extract_signals(
                    data_graph=None,
                    results_graph=None,
                    parse_ok=False,
                    parse_serialization=serialization,
                ),
                stats={"parse_serialization": serialization},
            )

        # Run SHACL.
        results_graph, shacl_error = engine.run_shacl_validation(
            data_graph,
            merged_shapes,
            merged_ontology,
            inference_mode=settings["inference_mode"],
            advanced_shacl=settings["advanced_shacl"],
        )
        if results_graph is None:
            return ValidationResult(
                passed=False,
                issues=[
                    *bundle_warnings,
                    ValidationIssue(
                        path="",
                        message=shacl_error or _("SHACL engine error."),
                        severity=Severity.ERROR,
                        code="shacl.engine_error",
                    ),
                ],
                signals=engine.extract_signals(
                    data_graph=data_graph,
                    results_graph=None,
                    parse_ok=True,
                    parse_serialization=serialization,
                ),
                stats={"parse_serialization": serialization},
            )

        # Map findings + signals.
        shacl_issues = engine.map_results_to_issues(results_graph)

        # Execute author-defined SPARQL ASK assertions after SHACL
        # completes. Each false answer produces a finding at the
        # severity the author configured; engine-level failures
        # (timeouts, scrub rejections, runtime errors) always produce
        # an ERROR finding regardless of configured severity.
        sparql_assertions = engine.parse_sparql_assertions(
            self._resolve_sparql_assertions(validator, ruleset),
        )
        sparql_issues = engine.evaluate_sparql_assertions(
            assertions=sparql_assertions,
            data_graph=data_graph,
            results_graph=results_graph,
        )

        all_issues: list[ValidationIssue] = [
            *bundle_warnings,
            *shacl_issues,
            *sparql_issues,
        ]
        signals = engine.extract_signals(
            data_graph=data_graph,
            results_graph=results_graph,
            parse_ok=True,
            parse_serialization=serialization,
        )

        # Serialise the native SHACL ValidationReport for download. Stored
        # under stats so the existing run-detail UI can surface it as an
        # evidence artifact without schema changes.
        try:
            report_turtle = results_graph.serialize(format="turtle")
        except Exception as exc:
            logger.warning("Failed to serialise SHACL report as Turtle: %s", exc)
            report_turtle = ""

        passed = not any(i.severity == Severity.ERROR for i in all_issues)

        return ValidationResult(
            passed=passed,
            issues=all_issues,
            signals=signals,
            stats={
                "parse_serialization": serialization,
                "triple_count": signals["triple_count"],
                "shacl_total_count": signals["shacl_total_count"],
                "results_graph_turtle": report_turtle,
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_settings(
        self,
        validator: Validator,
        ruleset: Ruleset | None,
    ) -> dict[str, Any]:
        """Pull shapes/ontology text and engine settings from both rulesets.

        Library-level custom SHACL validators carry their bundled
        standard shapes on ``validator.default_ruleset``. The step's own
        ``ruleset`` (always present for SHACL because
        ``supports_assertions=True``) carries project-specific extras
        plus the engine knobs (inference mode, advanced flag, submission
        format).

        Returns a flat dict consumed by the orchestrator above.
        """
        default_ruleset = getattr(validator, "default_ruleset", None)
        default_metadata = self._safe_metadata(default_ruleset)
        step_metadata = self._safe_metadata(ruleset)

        # Engine knobs: step-level value wins if explicitly set; otherwise
        # inherit from the library validator's default_ruleset; otherwise
        # fall back to the SHACLValidator defaults documented in the ADR.
        return {
            "default_shapes_text": (
                getattr(default_ruleset, "rules", "") if default_ruleset else ""
            ),
            "default_ontology_text": default_metadata.get("ontology_text", "") or "",
            "default_bundled_standards": default_metadata.get("bundled_standards"),
            "step_shapes_text": getattr(ruleset, "rules", "") if ruleset else "",
            "step_ontology_text": step_metadata.get("ontology_text", "") or "",
            "step_bundled_standards": step_metadata.get("bundled_standards"),
            "inference_mode": self._pick_setting(
                step_metadata,
                default_metadata,
                "inference_mode",
                "rdfs",
            ),
            "advanced_shacl": self._pick_setting(
                step_metadata,
                default_metadata,
                "advanced_shacl",
                fallback=True,
            ),
            "submission_format": self._pick_setting(
                step_metadata,
                default_metadata,
                "submission_format",
                "auto",
            ),
        }

    @staticmethod
    def _safe_metadata(ruleset: Ruleset | None) -> dict[str, Any]:
        if ruleset is None:
            return {}
        meta = getattr(ruleset, "metadata", None) or {}
        if not isinstance(meta, dict):
            return {}
        return meta

    def _resolve_sparql_assertions(
        self,
        validator: Validator,
        ruleset: Ruleset | None,
    ) -> list[Any]:
        """Merge library-validator + step-level SPARQL ASK assertions.

        Mirrors the merge pattern used for shapes / ontologies: the
        library validator's ``default_ruleset`` provides the baseline
        assertion list; the step-level ruleset's list extends it.
        Authors who want to override a library-level assertion add a
        new assertion with their own message at the step level —
        Validibot evaluates every entry from both lists.

        The raw lists live in ``Ruleset.metadata["sparql_assertions"]``
        and are stored as plain dicts (one per assertion). Returning
        the merged dict list rather than parsed dataclasses keeps the
        engine the only place that validates the shape.
        """
        default_ruleset = getattr(validator, "default_ruleset", None)
        default_metadata = self._safe_metadata(default_ruleset)
        step_metadata = self._safe_metadata(ruleset)

        merged: list[Any] = []
        default_list = default_metadata.get("sparql_assertions") or []
        step_list = step_metadata.get("sparql_assertions") or []
        if isinstance(default_list, list):
            merged.extend(default_list)
        if isinstance(step_list, list):
            merged.extend(step_list)
        return merged

    @staticmethod
    def _pick_setting(
        step_metadata: dict[str, Any],
        default_metadata: dict[str, Any],
        key: str,
        fallback: Any,
    ) -> Any:
        """Step value wins if explicitly set; else library default; else fallback."""
        if key in step_metadata:
            return step_metadata[key]
        if key in default_metadata:
            return default_metadata[key]
        return fallback
