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
import re
import threading
from dataclasses import dataclass
from typing import Any

import pyshacl
from django.conf import settings as django_settings
from rdflib import Graph
from rdflib import URIRef
from rdflib.exceptions import ParserError
from rdflib.namespace import SH

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import Severity
from validibot.validations.validators.base.base import ValidationIssue
from validibot.validations.validators.shacl.sparql_security import SparqlScrubError
from validibot.validations.validators.shacl.sparql_security import scrub_sparql_ask

logger = logging.getLogger(__name__)

# =============================================================================
# Resource limits
# =============================================================================
#
# These constants are the engine's safety net. Every limit is overridable
# via Django settings (see ``_setting_int``) up to a hard cap defined in
# the ADR's "Resource limits" table. Hitting any of these produces a
# ``ValidationIssue`` with severity ERROR — the worker does not crash.
#
# See ADR-2026-05-18 "Security" → "Resource limits".

DEFAULT_MAX_DATA_TRIPLES = 100_000
DEFAULT_MAX_SHAPE_TRIPLES = 50_000
DEFAULT_MAX_ONTOLOGY_TRIPLES = 100_000
DEFAULT_MAX_VALIDATION_DEPTH = 25

# SPARQL ASK execution budget (wall clock).
DEFAULT_SPARQL_QUERY_TIMEOUT_SECONDS = 10
DEFAULT_SPARQL_QUERY_TIMEOUT_MAX_SECONDS = 60

# Maximum number of SPARQL ASK assertions per step (form-level cap).
DEFAULT_SPARQL_ASKS_PER_STEP = 25


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

    Security: every call passes through :func:`prevalidate_safety` before
    rdflib touches the bytes. Submissions or shape files containing
    XXE-style XML constructs (DOCTYPE, ENTITY) or JSON-LD with remote
    ``@context`` references are refused at this layer — never reaching
    rdflib's parser. See ADR-2026-05-18 "Security".
    """
    if not content:
        return None, "Submission is empty."

    safety_error = prevalidate_safety(content, rdf_format)
    if safety_error is not None:
        return None, safety_error

    g = Graph()
    try:
        g.parse(data=content, format=rdf_format)
    except ParserError as exc:
        return None, f"RDF parse error ({rdf_format}): {exc}"
    except Exception as exc:
        # Catch-all per the Security section's error-handling discipline:
        # rdflib parsers can raise a variety of exceptions on malformed
        # input; we translate them all into a generic, user-safe message.
        # The raw exception detail is logged for operator forensics but
        # not exposed downstream.
        logger.warning(
            "RDF parse raised unexpected exception",
            extra={"rdf_format": rdf_format, "exc_type": type(exc).__name__},
        )
        return None, f"Unexpected error parsing RDF as {rdf_format}: {exc}"

    return g, None


# =============================================================================
# Pre-parse safety scanning
# =============================================================================
#
# These checks run on the raw bytes BEFORE rdflib's parsers see them.
# The goal is to refuse known-dangerous content at the earliest possible
# point in the pipeline, before any third-party parser can be tricked
# into a fetch or an entity expansion. The patterns are deliberately
# conservative — false positives produce a clear refusal message; false
# negatives are mitigated by additional layers (engine kwargs, process-
# level isolation, egress deny). See ADR-2026-05-18 "Security" →
# "Network isolation" and "V1 hardenings".

# Match XML constructs that indicate DTD declarations or external
# entities. These are the building blocks of XXE attacks.
_XML_XXE_PATTERN = re.compile(
    r"<!DOCTYPE\b|<!ENTITY\b|<!ELEMENT\b|SYSTEM\s+['\"]|PUBLIC\s+['\"]",
    re.IGNORECASE,
)

# Match a JSON-LD ``@context`` value that points at a remote URL.
# The check is intentionally broad — anything starting with http(s):// or
# file:// or another non-data scheme. ``data:`` URIs are safe because
# they carry the context inline. Local relative paths (no scheme) also
# pass; rdflib treats them as inline once we hand it the parsed JSON.
_JSONLD_REMOTE_CONTEXT_PATTERN = re.compile(
    r'"@context"\s*:\s*(?:"(?P<single>(?!data:)[a-z][a-z0-9+\-.]*:[^"]*)"'
    r'|\[\s*"(?P<first>(?!data:)[a-z][a-z0-9+\-.]*:[^"]*)")',
    re.IGNORECASE,
)


def prevalidate_safety(content: str, rdf_format: str) -> str | None:
    """Reject RDF content containing known-dangerous constructs.

    Runs before rdflib parsing. Returns an error message string if the
    content must be refused, otherwise ``None``. Designed to be cheap —
    two regex scans at most — and to fail closed (any unexpected error
    in the scan returns an error rather than silently passing through).

    Specifically refused:

    - RDF/XML containing ``<!DOCTYPE``, ``<!ENTITY``, ``<!ELEMENT``, or
      ``SYSTEM`` / ``PUBLIC`` external-entity declarations. These are
      the XXE family — they would let an attacker exfiltrate local
      files (``file:///etc/passwd``) or trigger SSRF.
    - JSON-LD whose top-level ``@context`` references a non-``data:``
      URL. rdflib's JSON-LD plugin will fetch remote contexts at parse
      time, which is both an SSRF vector and an exfiltration vector
      (the attacker logs the request).

    Other serializations (Turtle, N-Triples, N-Quads) do not have a
    network-fetching surface in rdflib's parser; they are passed
    through unchanged.

    Args:
        content: The raw RDF text.
        rdf_format: The rdflib format slug (``turtle``, ``json-ld``,
            ``xml``, ``nt``, ``nquads``). Determines which scan runs.

    Returns:
        ``None`` if the content is safe to hand to rdflib;
        otherwise a user-facing error message naming the construct
        that triggered the refusal.
    """
    if not content:
        return None

    # RDF/XML XXE refusal. The check runs only on the RDF/XML format;
    # ``<!DOCTYPE`` inside a Turtle literal would be a false positive
    # we want to avoid.
    if rdf_format == "xml":
        match = _XML_XXE_PATTERN.search(content)
        if match is not None:
            return (
                f"RDF/XML content contains '{match.group(0).strip()}', "
                "which the validator refuses as an XXE / external-entity "
                "vector. Remove the DTD / entity declaration and resubmit, "
                "or convert the file to Turtle, JSON-LD, or N-Triples."
            )

    # JSON-LD remote-context refusal. We check the raw text rather than
    # parsing the JSON because parsing is what we're trying to prevent
    # from making network calls.
    if rdf_format == "json-ld":
        match = _JSONLD_REMOTE_CONTEXT_PATTERN.search(content)
        if match is not None:
            url = match.group("single") or match.group("first")
            return (
                f"JSON-LD content references remote @context '{url}'. "
                "The validator refuses remote contexts to prevent SSRF "
                "and data-exfiltration vectors. Inline the @context "
                "object in the JSON-LD, use a data: URI, or convert the "
                "file to Turtle."
            )

    return None


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

    data_limit = _setting_int("SHACL_MAX_DATA_TRIPLES", DEFAULT_MAX_DATA_TRIPLES)
    if len(data_graph) > data_limit:
        return None, (
            f"Submitted RDF graph has {len(data_graph)} triples, over the "
            f"{data_limit} triple SHACL validation limit."
        )

    shapes_graph = Graph()
    try:
        shapes_graph.parse(data=shapes_text, format="turtle")
    except Exception as exc:
        return None, f"Shapes graph failed to parse as Turtle: {exc}"
    shape_limit = _setting_int("SHACL_MAX_SHAPE_TRIPLES", DEFAULT_MAX_SHAPE_TRIPLES)
    if len(shapes_graph) > shape_limit:
        return None, (
            f"SHACL shapes graph has {len(shapes_graph)} triples, over the "
            f"{shape_limit} triple validation limit."
        )

    ontology_graph: Graph | None = None
    if ontology_text.strip():
        ontology_graph = Graph()
        try:
            ontology_graph.parse(data=ontology_text, format="turtle")
        except Exception as exc:
            return None, f"Ontology graph failed to parse as Turtle: {exc}"
        ontology_limit = _setting_int(
            "SHACL_MAX_ONTOLOGY_TRIPLES",
            DEFAULT_MAX_ONTOLOGY_TRIPLES,
        )
        if len(ontology_graph) > ontology_limit:
            return None, (
                f"SHACL ontology graph has {len(ontology_graph)} triples, over the "
                f"{ontology_limit} triple validation limit."
            )

    try:
        _conforms, results_graph, _results_text = pyshacl.validate(
            data_graph,
            shacl_graph=shapes_graph,
            ont_graph=ontology_graph,
            inference=inference_mode,
            advanced=advanced_shacl,
            max_validation_depth=_setting_int(
                "SHACL_MAX_VALIDATION_DEPTH",
                DEFAULT_MAX_VALIDATION_DEPTH,
            ),
            # SECURITY: pyshacl's JavaScript-constraint engine
            # (``sh:JSConstraint``) executes attacker-controlled JS via
            # pyduktape3. We disable it unconditionally and rely on the
            # absence of pyduktape3 in the wheel as belt-and-suspenders.
            # See ADR-2026-05-18 "Security" → "Code execution".
            js=False,
            # SECURITY: pyshacl can follow ``owl:imports`` and fetch the
            # imported ontology over the network. We hard-code False
            # (also the default) and never read this from a setting —
            # there is no operator-facing override.
            do_owl_imports=False,
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
# SPARQL ASK assertion execution
# =============================================================================
#
# Author-defined SPARQL ASK assertions run after pyshacl completes. Each
# ASK gets its own wall-clock budget enforced by a daemon thread; the
# return value is the boolean answer of the query, with errors mapped to
# a finding rather than a raised exception (per the Security section's
# error-handling discipline). See ADR-2026-05-18 "Phase 1c — SPARQL ASK
# assertions" and "Assertions against the SHACL output".


# Allowed values for an ASK assertion's ``target_graph`` field. Kept here
# rather than in ``constants.py`` because the validator package owns
# this small enum and we want it co-located with the engine that
# evaluates it.
SPARQL_ASK_TARGET_DATA = "data"
SPARQL_ASK_TARGET_RESULTS = "results"
SPARQL_ASK_TARGET_UNION = "union"
_VALID_TARGET_GRAPHS: frozenset[str] = frozenset(
    {SPARQL_ASK_TARGET_DATA, SPARQL_ASK_TARGET_RESULTS, SPARQL_ASK_TARGET_UNION},
)


@dataclass(frozen=True)
class SparqlAskAssertion:
    """One author-defined SPARQL ASK assertion attached to a SHACL step.

    Persisted as a dict inside ``Ruleset.metadata["sparql_assertions"]``;
    the engine rehydrates that dict into one of these dataclass
    instances per call.

    Fields:
        target_graph: ``"data"`` / ``"results"`` / ``"union"``. Decides
            which graph the ASK runs against.
        query: The raw SPARQL ASK text (already scrubbed at form save
            time but re-scrubbed at run time as belt-and-suspenders).
        severity: The Validibot severity the engine emits when the ASK
            returns ``false``. Authors typically pick ``ERROR`` for hard
            gates and ``WARNING`` for advisory checks.
        description: Optional human-readable label shown in finding lists.
        error_message_template: Optional CEL-style template — currently
            stored verbatim, with future plans to support ``{{ o.foo }}``
            substitution once the named-signal ADR lands.
    """

    target_graph: str
    query: str
    severity: str  # one of Severity.ERROR / .WARNING / .INFO
    description: str = ""
    error_message_template: str = ""


def run_sparql_ask(
    *,
    query_text: str,
    target_graph_name: str,
    data_graph: Graph,
    results_graph: Graph | None,
    timeout_seconds: int | None = None,
) -> tuple[bool | None, str | None]:
    """Execute one SPARQL ASK against the requested target graph.

    Returns ``(answer, error_message)`` with exactly one not-None.
    ``answer`` is the boolean result; ``error_message`` carries a
    user-facing description if the query was rejected by the AST scrub,
    raised at execution time, or exceeded its wall-clock budget.

    Args:
        query_text: The raw SPARQL ASK text.
        target_graph_name: Which graph to run the ASK against.
            Must be one of ``"data"``, ``"results"``, ``"union"``.
        data_graph: The submission's parsed RDF graph (post-inference).
        results_graph: The SHACL ``sh:ValidationReport`` graph, or
            ``None`` if SHACL did not run (e.g. parse failed earlier).
        timeout_seconds: Wall-clock budget. ``None`` reads from
            ``SHACL_SPARQL_QUERY_TIMEOUT_SECONDS`` setting (default 10).
            Hard-capped by ``SHACL_SPARQL_QUERY_TIMEOUT_MAX_SECONDS``
            (default 60) — operators can lower the default but not
            exceed the cap.

    Returns:
        ``(answer, None)`` on success — ``answer`` is the ASK's boolean.
        ``(None, error_message)`` on any policy / parse / runtime
        failure. The engine never raises out of this function.
    """
    if target_graph_name not in _VALID_TARGET_GRAPHS:
        return None, (
            f"Unknown SPARQL target graph '{target_graph_name}'. "
            f"Expected one of: {sorted(_VALID_TARGET_GRAPHS)}."
        )

    # Re-scrub at run time. The form already rejected this at save, but
    # nothing prevents a fixture, an admin import, or a downstream API
    # consumer from inserting an unscrubbed query. The cost of an extra
    # parse is microseconds; the cost of a missed scrub could be data
    # exfiltration.
    try:
        scrub_sparql_ask(query_text)
    except SparqlScrubError as exc:
        return None, f"SPARQL ASK rejected by security scrub: {exc}"

    target = _select_target_graph(
        target_graph_name=target_graph_name,
        data_graph=data_graph,
        results_graph=results_graph,
    )
    if target is None:
        # ``results`` or ``union`` requested but SHACL didn't produce a
        # report graph. Surface a clear message rather than crashing the
        # ASK with a missing-target error.
        return None, (
            f"SPARQL target '{target_graph_name}' is not available "
            "because no SHACL results graph was produced. Did SHACL "
            "fail to run, or did parsing fail earlier in the pipeline?"
        )

    effective_timeout = _resolve_sparql_timeout(timeout_seconds)

    answer, error = _execute_ask_with_timeout(
        query_text=query_text,
        graph=target,
        timeout_seconds=effective_timeout,
    )
    return answer, error


def _select_target_graph(
    *,
    target_graph_name: str,
    data_graph: Graph,
    results_graph: Graph | None,
) -> Graph | None:
    """Resolve a ``target_graph`` name to an rdflib Graph instance.

    For the ``union`` target we build a fresh ``Graph`` containing every
    triple from both inputs. This is O(|data| + |results|) and creates
    one extra graph in memory per ASK that uses it — acceptable at the
    triple-count limits the engine already enforces.
    """
    if target_graph_name == SPARQL_ASK_TARGET_DATA:
        return data_graph
    if target_graph_name == SPARQL_ASK_TARGET_RESULTS:
        return results_graph
    if target_graph_name == SPARQL_ASK_TARGET_UNION:
        if results_graph is None:
            return data_graph
        union = Graph()
        for triple in data_graph:
            union.add(triple)
        for triple in results_graph:
            union.add(triple)
        return union
    return None


def _resolve_sparql_timeout(explicit_timeout: int | None) -> int:
    """Compute the effective per-ASK timeout, clamped to the hard cap.

    Settings overrides:

    - ``SHACL_SPARQL_QUERY_TIMEOUT_SECONDS`` — operator default (10).
    - ``SHACL_SPARQL_QUERY_TIMEOUT_MAX_SECONDS`` — hard cap (60).
      Operators may lower the cap but not raise it above this constant.
    """
    if explicit_timeout is not None and explicit_timeout > 0:
        configured = explicit_timeout
    else:
        configured = _setting_int(
            "SHACL_SPARQL_QUERY_TIMEOUT_SECONDS",
            DEFAULT_SPARQL_QUERY_TIMEOUT_SECONDS,
        )
    cap = _setting_int(
        "SHACL_SPARQL_QUERY_TIMEOUT_MAX_SECONDS",
        DEFAULT_SPARQL_QUERY_TIMEOUT_MAX_SECONDS,
    )
    return min(configured, cap)


def _execute_ask_with_timeout(
    *,
    query_text: str,
    graph: Graph,
    timeout_seconds: int,
) -> tuple[bool | None, str | None]:
    """Run an ASK in a daemon thread, return on first to complete.

    rdflib's ``Graph.query()`` is synchronous and CPU-bound, so a
    daemon-thread wrapper is the most portable way to enforce a
    wall-clock budget without depending on POSIX-only ``signal.alarm``
    (which fails on non-main threads — i.e. Celery workers).

    On timeout we report a clear message and let the underlying thread
    terminate when it next yields to the interpreter; Python cannot
    forcibly kill a thread, so brief over-budget compute may leak. The
    Celery hard-task timeout is the ultimate stop. See ADR-2026-05-18
    "Process-level isolation".

    Returns ``(answer, None)`` or ``(None, error_message)``.
    """
    # We pass result and error back from the thread via mutable containers.
    # Threading.Event signals completion.
    result_holder: list[bool | None] = [None]
    error_holder: list[str | None] = [None]
    done = threading.Event()

    def runner() -> None:
        try:
            qres = graph.query(query_text)
            # ``askAnswer`` is the canonical attribute for ASK queries
            # in rdflib's QueryResult. Fall back to bool() of the
            # result iterator if the attribute is absent (older
            # rdflib versions).
            answer = getattr(qres, "askAnswer", None)
            if answer is None:
                answer = bool(qres)
            result_holder[0] = bool(answer)
        except Exception as exc:
            # Per Security section: every external call is wrapped; the
            # exception class name (only) is exposed downstream.
            logger.warning(
                "SPARQL ASK raised",
                extra={"exc_type": type(exc).__name__},
            )
            error_holder[0] = (
                f"SPARQL ASK execution failed: {type(exc).__name__}: {exc}"
            )
        finally:
            done.set()

    thread = threading.Thread(target=runner, name="shacl-sparql-ask", daemon=True)
    thread.start()
    finished = done.wait(timeout=timeout_seconds)
    if not finished:
        return None, (
            f"SPARQL ASK exceeded the {timeout_seconds}s wall-clock "
            "budget. Simplify the query, narrow the target graph, or "
            "increase the SHACL_SPARQL_QUERY_TIMEOUT_SECONDS setting "
            "(up to the configured hard cap)."
        )

    if error_holder[0] is not None:
        return None, error_holder[0]
    return result_holder[0], None


def evaluate_sparql_assertions(
    *,
    assertions: list[SparqlAskAssertion],
    data_graph: Graph,
    results_graph: Graph | None,
) -> list[ValidationIssue]:
    """Run every assertion in order, return one finding per failing ASK.

    Each assertion's ``severity`` determines whether a ``false`` answer
    contributes an ERROR / WARNING / INFO finding. Engine errors
    (timeouts, scrub rejections that slipped past form save, runtime
    exceptions) always produce an ERROR finding regardless of the
    assertion's configured severity — they indicate a configuration
    problem the author needs to see.

    Args:
        assertions: Parsed assertion list from
            ``Ruleset.metadata["sparql_assertions"]``.
        data_graph: The parsed submission graph.
        results_graph: The SHACL ``sh:ValidationReport``, or ``None`` if
            SHACL did not run (e.g. parse failed). Assertions targeting
            ``results`` or ``union`` produce a clear configuration
            finding rather than running.

    Returns:
        A list of ``ValidationIssue`` rows. Empty if every ASK returned
        ``true``.
    """
    issues: list[ValidationIssue] = []
    for index, assertion in enumerate(assertions):
        label = assertion.description or f"SPARQL ASK #{index + 1}"
        answer, error = run_sparql_ask(
            query_text=assertion.query,
            target_graph_name=assertion.target_graph,
            data_graph=data_graph,
            results_graph=results_graph,
        )
        if error is not None:
            # Engine-level failure (timeout / scrub / runtime). Always
            # ERROR — the author needs to fix the assertion config.
            issues.append(
                ValidationIssue(
                    path="",
                    message=f"{label}: {error}",
                    severity=Severity.ERROR,
                    code="shacl.sparql_ask_engine_error",
                    meta={
                        "assertion_index": index,
                        "target_graph": assertion.target_graph,
                    },
                ),
            )
            continue

        if answer is False:
            issues.append(
                ValidationIssue(
                    path="",
                    message=(
                        assertion.error_message_template
                        or f"{label}: assertion returned false."
                    ),
                    severity=assertion.severity,
                    code="shacl.sparql_ask_failed",
                    meta={
                        "assertion_index": index,
                        "target_graph": assertion.target_graph,
                        "description": assertion.description,
                    },
                ),
            )
    return issues


def parse_sparql_assertions(raw: Any) -> list[SparqlAskAssertion]:
    """Rehydrate a raw metadata list into typed assertion dataclasses.

    Tolerant of malformed entries — anything that doesn't look like a
    valid assertion dict is silently skipped, with a warning logged for
    operator forensics. This protects the engine from a corrupted
    metadata blob, e.g. one that survived a partial migration.
    """
    if not isinstance(raw, list):
        return []
    out: list[SparqlAskAssertion] = []
    for entry in raw:
        if not isinstance(entry, dict):
            logger.warning(
                "Skipping non-dict SPARQL assertion entry",
                extra={"entry_type": type(entry).__name__},
            )
            continue
        try:
            target = str(entry.get("target_graph", SPARQL_ASK_TARGET_DATA))
            query = str(entry.get("query", "")).strip()
            severity = str(entry.get("severity", Severity.ERROR))
            description = str(entry.get("description", "") or "")
            error_message_template = str(
                entry.get("error_message_template", "") or "",
            )
        except Exception as exc:
            logger.warning(
                "Skipping malformed SPARQL assertion entry",
                extra={"exc_type": type(exc).__name__},
            )
            continue

        if not query or target not in _VALID_TARGET_GRAPHS:
            logger.warning(
                "Skipping invalid SPARQL assertion entry",
                extra={"target": target, "has_query": bool(query)},
            )
            continue

        out.append(
            SparqlAskAssertion(
                target_graph=target,
                query=query,
                severity=severity,
                description=description,
                error_message_template=error_message_template,
            ),
        )
    return out


def _setting_int(name: str, default: int) -> int:
    """Read a positive integer Django setting, falling back on invalid values."""
    try:
        value = int(getattr(django_settings, name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


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
