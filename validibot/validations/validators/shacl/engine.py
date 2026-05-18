"""Pure SHACL engine functions, separated from the validator class.

Splitting these out of ``validator.py`` lets us unit-test each step in
isolation without instantiating Django models:

- :func:`parse_rdf` — parse RDF in any supported serialization.
- :func:`detect_serialization` — pick an rdflib format from submission metadata.
- :func:`merge_shapes_and_ontologies` — combine library-validator defaults with
  step-level extras (mirrors the assertion-merge in ``BaseValidator``).
- :func:`load_bundled_standards` — load Brick / QUDT bundles (Phase 1 is a
  no-op stub; Phase 2 ships the bundle content).
- :func:`run_shacl_validation` — orchestrate the ``pyshacl.validate`` call.
- :func:`map_results_to_issues` — walk the SHACL ValidationReport graph and
  emit ``ValidationIssue`` rows mapped to Validibot severity.
- :func:`extract_signals` — compute the output signals (``o.*``) for CEL
  assertions.

See ADR-2026-05-18 for the architecture rationale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import pyshacl
from rdflib import Graph
from rdflib import URIRef
from rdflib.exceptions import ParserError
from rdflib.namespace import SH

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import Severity
from validibot.validations.validators.base.base import ValidationIssue

logger = logging.getLogger(__name__)


# Well-known building-domain namespaces. Used by signal extraction to
# detect whether the submitted graph mentions ASHRAE 223P, Guideline 36,
# or Brick — useful as a coarse routing signal for CEL assertions.
NS_S223 = "http://data.ashrae.org/standard223#"
NS_G36 = "http://data.ashrae.org/standard223/1.0/extensions/g36#"
NS_BRICK = "https://brickschema.org/schema/Brick#"


# Map rdflib format slugs to filename extensions and submission file types.
# rdflib accepts "turtle", "json-ld", "xml" (RDF/XML), "nt" (N-Triples),
# "nquads", "n3", "trig". For Validibot we ship the five most common.
_EXTENSION_TO_FORMAT: dict[str, str] = {
    "ttl": "turtle",
    "turtle": "turtle",
    "n3": "n3",
    "jsonld": "json-ld",
    "json-ld": "json-ld",
    "rdf": "xml",
    "rdfxml": "xml",
    "xml": "xml",
    "nt": "nt",
    "ntriples": "nt",
    "nq": "nquads",
    "nquads": "nquads",
}


# SHACL severity (rdflib URI) → Validibot Severity enum value.
_SH_SEVERITY_TO_VALIDIBOT: dict[URIRef, str] = {
    SH.Violation: Severity.ERROR,
    SH.Warning: Severity.WARNING,
    SH.Info: Severity.INFO,
}


@dataclass(frozen=True)
class ShaclSettings:
    """Engine configuration resolved from the merged ruleset metadata.

    All knobs the engine reads at validation time. Populated from the
    step ruleset's ``metadata`` dict (which the form's builder helper
    writes when the workflow author saves the step config).
    """

    shapes_text: str
    ontology_text: str
    bundled_standards: list[str]
    inference_mode: str  # "none" | "rdfs" | "owlrl"
    advanced_shacl: bool
    submission_format: str  # "auto" | "turtle" | "jsonld" | "rdfxml" | "nt" | "nquads"


# =============================================================================
# RDF parsing
# =============================================================================


def detect_serialization(
    file_name: str | None,
    file_type: str | None,
    explicit_format: str | None,
) -> str:
    """Pick an rdflib format string from submission metadata.

    Priority: explicit format > file extension > submission file_type >
    default ("turtle"). The validator falls back to Turtle because it is
    the most common serialization for SHACL shapes and 223P models.

    Args:
        file_name: Submission filename, used to read the extension.
        file_type: ``SubmissionFileType`` value (JSON / XML / TEXT / ...).
        explicit_format: Operator-supplied override from the step config.
            Pass ``None`` or "auto" to enable auto-detection.

    Returns:
        rdflib format string ("turtle", "json-ld", "xml", "nt", "nquads").
    """
    if explicit_format and explicit_format != "auto":
        return _EXTENSION_TO_FORMAT.get(explicit_format.lower(), "turtle")

    if file_name and "." in file_name:
        ext = file_name.rsplit(".", 1)[-1].lower()
        if ext in _EXTENSION_TO_FORMAT:
            return _EXTENSION_TO_FORMAT[ext]

    # Fall back to broad SubmissionFileType signal. JSON is overwhelmingly
    # JSON-LD in the RDF world; XML is RDF/XML. TEXT and everything else
    # defaults to Turtle (the most common serialization).
    if file_type == SubmissionFileType.JSON:
        return "json-ld"
    if file_type == SubmissionFileType.XML:
        return "xml"

    return "turtle"


def parse_rdf(content: str, rdf_format: str) -> tuple[Graph | None, str | None]:
    """Parse RDF content into an rdflib ``Graph``.

    Returns ``(graph, error_message)`` where exactly one is ``None``.
    On parse failure the error message is suitable for surfacing as a
    ``ValidationIssue`` with severity ERROR.
    """
    if not content:
        return None, "Submission is empty."

    g = Graph()
    try:
        g.parse(data=content, format=rdf_format)
    except ParserError as exc:
        return None, f"RDF parse error ({rdf_format}): {exc}"
    except Exception as exc:
        return None, f"Unexpected error parsing RDF as {rdf_format}: {exc}"

    return g, None


# =============================================================================
# Shape and ontology merging
# =============================================================================


# Separator placed between concatenated files inside ``Ruleset.rules_text``
# and ``metadata["ontology_text"]``. The form builder writes the same
# marker so the engine can recover file boundaries if needed for
# diagnostics. The exact value is not part of the engine's contract.
FILE_SEPARATOR = "\n# === File boundary ===\n"


def merge_shapes_and_ontologies(
    default_shapes_text: str,
    default_ontology_text: str,
    default_bundled_standards: list[str] | None,
    step_shapes_text: str,
    step_ontology_text: str,
    step_bundled_standards: list[str] | None,
) -> tuple[str, str, list[str]]:
    """Merge library-validator defaults with step-level extras.

    Mirrors the assertion-merge pattern in
    :meth:`BaseValidator.evaluate_assertions_for_stage` — library
    defaults come first, step-level extras layer on top.

    For ``bundled_standards``, the step-level list (if any non-empty)
    overrides the library default. If the workflow author wants to opt
    out of a bundle the library validator included, they pass an
    explicit empty list at the step level; if they want to inherit, they
    leave it unset.

    Returns:
        ``(merged_shapes_text, merged_ontology_text, bundled_standards)``.
    """
    shapes_parts: list[str] = []
    if default_shapes_text:
        shapes_parts.append(default_shapes_text)
    if step_shapes_text:
        shapes_parts.append(step_shapes_text)
    shapes_text = FILE_SEPARATOR.join(shapes_parts)

    ontology_parts: list[str] = []
    if default_ontology_text:
        ontology_parts.append(default_ontology_text)
    if step_ontology_text:
        ontology_parts.append(step_ontology_text)
    ontology_text = FILE_SEPARATOR.join(ontology_parts)

    # Step bundled_standards wins if explicitly provided (even empty list
    # signals intentional opt-out). Otherwise inherit the library default.
    if step_bundled_standards is not None:
        bundled = step_bundled_standards
    else:
        bundled = default_bundled_standards or []

    return shapes_text, ontology_text, bundled


# =============================================================================
# Bundled standards
# =============================================================================


# Phase 1 placeholder. Phase 2 ships static assets under
# ``validibot/validations/validators/shacl/bundles/`` for Brick and QUDT
# (license-clean per the ADR) and this function returns their content.
# ASHRAE 223P is never bundled (operators upload from their own copy).
_KNOWN_BUNDLES = {"brick-1.4", "qudt-2.1"}


def load_bundled_standards(
    bundled_standards: list[str],
) -> tuple[str, str, list[ValidationIssue]]:
    """Load shapes + ontology content for the requested bundled standards.

    Phase 1 returns empty content and emits a WARNING issue for every
    requested bundle so workflow authors who opted into Brick or QUDT in
    the form see a clear gap. Phase 2 fills in the static assets and
    this function returns real content.

    Returns:
        ``(bundled_shapes_text, bundled_ontology_text, warning_issues)``.
    """
    warnings: list[ValidationIssue] = []
    for bundle in bundled_standards:
        if bundle in _KNOWN_BUNDLES:
            warnings.append(
                ValidationIssue(
                    path="",
                    message=(
                        f"Bundled standard '{bundle}' is recognised but the "
                        "shapes file ships in Phase 2 of the SHACL validator "
                        "rollout. The validation will proceed without these "
                        "shapes. Upload the file manually if you need it now."
                    ),
                    severity=Severity.WARNING,
                    code="shacl.bundle_not_yet_shipped",
                ),
            )
        else:
            warnings.append(
                ValidationIssue(
                    path="",
                    message=(
                        f"Unknown bundled standard '{bundle}'. The validator "
                        "does not recognise this identifier; check the step "
                        "config or upload the shapes manually."
                    ),
                    severity=Severity.WARNING,
                    code="shacl.bundle_unknown",
                ),
            )
    return "", "", warnings


# =============================================================================
# SHACL validation
# =============================================================================


def run_shacl_validation(
    data_graph: Graph,
    shapes_text: str,
    ontology_text: str,
    *,
    inference_mode: str,
    advanced_shacl: bool,
) -> tuple[Graph | None, str | None]:
    """Run pyshacl against the data graph using the supplied shapes.

    Builds the shapes graph and (optionally) the ontology graph from the
    supplied Turtle text, then delegates to ``pyshacl.validate``.

    Args:
        data_graph: The parsed submission graph.
        shapes_text: Concatenated SHACL shapes (Turtle).
        ontology_text: Optional ontology Turtle for inference. Pass ""
            when the shapes file is also the ontology (true for ASHRAE
            223P where classes are simultaneously sh:NodeShape).
        inference_mode: "none" | "rdfs" | "owlrl" | "both".
        advanced_shacl: Enable ``sh:SPARQLConstraint``, ``sh:JSConstraint``,
            and SHACL Rules. Required for ASHRAE 223P.

    Returns:
        ``(results_graph, error_message)`` — exactly one is ``None``.
        ``results_graph`` is a SHACL ``sh:ValidationReport`` graph that
        can be serialised to Turtle as an evidence artifact.
    """
    if not shapes_text.strip():
        return None, (
            "No SHACL shapes were supplied. Upload one or more shape "
            "files in the step config or attach a custom SHACL validator "
            "from the library."
        )

    shapes_graph = Graph()
    try:
        shapes_graph.parse(data=shapes_text, format="turtle")
    except Exception as exc:
        return None, f"Shapes graph failed to parse as Turtle: {exc}"

    ontology_graph: Graph | None = None
    if ontology_text.strip():
        ontology_graph = Graph()
        try:
            ontology_graph.parse(data=ontology_text, format="turtle")
        except Exception as exc:
            return None, f"Ontology graph failed to parse as Turtle: {exc}"

    try:
        _conforms, results_graph, _results_text = pyshacl.validate(
            data_graph,
            shacl_graph=shapes_graph,
            ont_graph=ontology_graph,
            inference=inference_mode,
            advanced=advanced_shacl,
            # Include Warning and Info findings in the report. Validibot
            # computes ``passed`` from severity counts after mapping, so
            # we want all findings in the report regardless of how
            # pyshacl interprets conformance.
            allow_warnings=True,
            allow_infos=True,
        )
    except Exception as exc:
        logger.exception("pyshacl.validate raised")
        return None, f"SHACL engine error: {exc}"

    return results_graph, None


# =============================================================================
# Result mapping
# =============================================================================


def map_results_to_issues(results_graph: Graph) -> list[ValidationIssue]:
    """Walk a SHACL ``sh:ValidationReport`` graph and emit findings.

    Each ``sh:ValidationResult`` node becomes one ``ValidationIssue``
    with severity mapped from ``sh:resultSeverity`` and SHACL-specific
    detail (focus node, source shape, constraint component, value)
    packed into ``meta`` for downstream display.

    Args:
        results_graph: The SHACL ValidationReport graph produced by
            :func:`run_shacl_validation`.

    Returns:
        List of ``ValidationIssue`` rows. Empty if the data graph
        conforms to the shapes.
    """
    issues: list[ValidationIssue] = []

    for result_node in results_graph.objects(predicate=SH.result):
        severity_uri = results_graph.value(result_node, SH.resultSeverity)
        severity = _SH_SEVERITY_TO_VALIDIBOT.get(severity_uri, Severity.ERROR)

        focus_node = results_graph.value(result_node, SH.focusNode)
        result_path = results_graph.value(result_node, SH.resultPath)
        source_shape = results_graph.value(result_node, SH.sourceShape)
        constraint = results_graph.value(
            result_node,
            SH.sourceConstraintComponent,
        )
        value = results_graph.value(result_node, SH.value)

        # ``sh:resultMessage`` may appear multiple times (one per language).
        # We grab the first one for the user-visible message and store the
        # whole set in ``meta`` for richer surfaces later.
        messages = [
            str(m) for m in results_graph.objects(result_node, SH.resultMessage)
        ]
        primary_message = messages[0] if messages else "SHACL constraint violated."

        meta: dict[str, Any] = {
            "shacl_focus_node": _node_repr(focus_node),
            "shacl_source_shape": _node_repr(source_shape),
            "shacl_constraint_component": _node_repr(constraint),
        }
        if value is not None:
            meta["shacl_value"] = _node_repr(value)
        if len(messages) > 1:
            meta["shacl_all_messages"] = messages

        issues.append(
            ValidationIssue(
                path=_node_repr(result_path) or "",
                message=primary_message,
                severity=severity,
                code=_shacl_code_from_constraint(constraint),
                meta=meta,
            ),
        )

    # Stable ordering: ERRORs first, then WARNINGs, then INFOs, then by
    # source_shape for determinism so test assertions don't flake.
    severity_order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
    issues.sort(
        key=lambda i: (
            severity_order.get(i.severity, 99),
            (i.meta or {}).get("shacl_source_shape", "") or "",
            i.message,
        ),
    )
    return issues


def _node_repr(node: Any) -> str:
    """Render an rdflib node as a stable string for finding paths/meta.

    ``URIRef`` → the URI itself. ``BNode`` → ``_:<id>``. ``Literal`` →
    the lexical form. ``None`` → empty string. This keeps downstream
    storage and display simple — Validibot does not need to round-trip
    the original rdflib node objects.
    """
    if node is None:
        return ""
    return str(node)


def _shacl_code_from_constraint(constraint_uri: Any) -> str:
    """Derive a short machine-readable code from a SHACL constraint URI.

    Strips the SHACL namespace prefix so e.g.
    ``http://www.w3.org/ns/shacl#MinCountConstraintComponent`` becomes
    ``shacl.MinCountConstraintComponent``. Keeps the full URI when the
    constraint comes from outside the SHACL namespace (custom
    SPARQLConstraint, etc.).
    """
    if constraint_uri is None:
        return "shacl.unknown"
    text = str(constraint_uri)
    if text.startswith(str(SH)):
        return f"shacl.{text[len(str(SH)) :]}"
    return f"shacl.{text}"


# =============================================================================
# Signal extraction
# =============================================================================


def extract_signals(
    data_graph: Graph | None,
    results_graph: Graph | None,
    *,
    parse_ok: bool,
    parse_serialization: str,
    inferred_triple_count: int = 0,
) -> dict[str, Any]:
    """Compute the ``o.*`` output signal dict for CEL assertions.

    Phase 1 ships the universal signals (parse, namespaces, SHACL counts).
    The 223P-specific signals (``o.equipment_count``,
    ``o.zones_with_co2_sensor_count``, etc.) land in Phase 2 once the
    bundled QUDT ontology unlocks unit-aware SPARQL queries.
    """
    signals: dict[str, Any] = {
        "parse_ok": parse_ok,
        "parse_serialization": parse_serialization,
        "triple_count": len(data_graph) if data_graph is not None else 0,
        "inferred_triple_count": inferred_triple_count,
        "namespaces_present": [],
        "has_s223_namespace": False,
        "has_g36_namespace": False,
        "has_brick_namespace": False,
        "shacl_violation_count": 0,
        "shacl_warning_count": 0,
        "shacl_info_count": 0,
        "shacl_total_count": 0,
    }

    if data_graph is not None:
        namespaces = _collect_namespaces(data_graph)
        signals["namespaces_present"] = sorted(namespaces)
        signals["has_s223_namespace"] = NS_S223 in namespaces
        signals["has_g36_namespace"] = NS_G36 in namespaces
        signals["has_brick_namespace"] = NS_BRICK in namespaces

    if results_graph is not None:
        for result_node in results_graph.objects(predicate=SH.result):
            sev = results_graph.value(result_node, SH.resultSeverity)
            if sev == SH.Violation:
                signals["shacl_violation_count"] += 1
            elif sev == SH.Warning:
                signals["shacl_warning_count"] += 1
            elif sev == SH.Info:
                signals["shacl_info_count"] += 1
        signals["shacl_total_count"] = (
            signals["shacl_violation_count"]
            + signals["shacl_warning_count"]
            + signals["shacl_info_count"]
        )

    return signals


def _collect_namespaces(graph: Graph) -> set[str]:
    """Return the set of namespace URIs that appear in any triple position.

    Iterates triples once. For a 50K-triple graph (our largest published
    223P example) this is fast; for million-triple graphs we'd want a
    SPARQL ``ASK`` per known namespace, but that optimisation can wait
    until a real customer brings such a graph.
    """
    seen: set[str] = set()
    for triple in graph:
        for term in triple:
            if isinstance(term, URIRef):
                uri = str(term)
                # Best-effort namespace split: take the URI up to the last
                # '#' or '/'. This is the conventional RDF prefix boundary.
                cut = max(uri.rfind("#"), uri.rfind("/"))
                if cut > 0:
                    seen.add(uri[: cut + 1])
    return seen
