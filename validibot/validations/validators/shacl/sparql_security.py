"""SPARQL ASK assertion security: parse-time AST scrubbing.

The :func:`scrub_sparql_ask` function is the single entry point. It
parses an author-supplied SPARQL ASK query, walks its algebra tree, and
returns ``None`` if the query is acceptable or a
:class:`SparqlScrubError` with a clear, user-facing message describing
what the author needs to fix.

Why this lives in its own module
--------------------------------

The scrub is called from two places:

1. Form ``clean()`` methods — to reject bad queries at save time before
   they ever reach the engine. This is the primary protection.
2. The engine's ``run_sparql_ask`` — as a belt-and-suspenders re-check
   right before execution, in case a query somehow reached persistence
   without passing through the form (e.g. a fixture or an admin import).

Keeping the scrub in a stand-alone, dependency-free module makes it
trivial to unit-test every forbidden construct in isolation. The form
and engine both call the same function, so any tightening here
propagates to both call sites.

What we forbid
--------------

Per ADR-2026-05-18 ("Security" → "SPARQL AST scrubbing"):

- Top-level form ≠ ``ASK``. ``SELECT``, ``CONSTRUCT``, ``DESCRIBE`` are
  rejected here. Update operations (``INSERT``, ``DELETE``, ``LOAD``,
  ``CLEAR``, ``CREATE``, ``DROP``, ``ADD``, ``MOVE``, ``COPY``) are
  rejected even earlier — ``parseQuery`` refuses to parse them at all
  because they belong to the SPARQL Update grammar. We rely on that
  refusal but check for the algebra-tree node names too, in case rdflib
  ever changes its parser surface.
- Any ``ServiceGraphPattern`` (the ``SERVICE`` clause) in the algebra
  tree. ``SERVICE`` is the canonical SPARQL federation exfiltration
  vector — left unblocked it would let an author POST data from the
  local graph to ``http://attacker.com/sparql``.
- ``FROM`` / ``FROM NAMED`` with non-default IRIs. These tell the SPARQL
  engine to load remote graphs. We do not support any remote-graph
  reference; the query must run against the data / results / union
  graphs the engine provides.
- Property paths whose serialised representation exceeds a configurable
  depth (default ``8``). A pattern like ``rdfs:subClassOf{1,99}`` or
  deeply nested alternations can cause cubic blowup on attacker-crafted
  hierarchies. The depth cap is a coarse but effective bound — most
  legitimate paths are 1–3 deep.
- Total query length above a cap (default ``10_000`` characters). Blunt
  but catches pathologically nested queries that slip past the other
  checks.

The function returns descriptive errors so authors can fix the query
without consulting documentation. Each error message names the
forbidden construct and points at the security rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings as django_settings
from rdflib.paths import AlternativePath
from rdflib.paths import InvPath
from rdflib.paths import MulPath
from rdflib.paths import NegatedPath
from rdflib.paths import Path
from rdflib.paths import SequencePath
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery
from rdflib.plugins.sparql.parserutils import CompValue

# Defaults match the resource-limits table in ADR-2026-05-18.
DEFAULT_MAX_QUERY_LENGTH = 10_000
DEFAULT_MAX_PROPERTY_PATH_DEPTH = 8
HARD_MAX_QUERY_LENGTH = 50_000
HARD_MAX_PROPERTY_PATH_DEPTH = 32

# Algebra-tree node names that must not appear anywhere in the query.
# rdflib's parser refuses Update operations entirely (see module
# docstring), but we still list their names here so a future parser
# surface change doesn't silently let them through.
_FORBIDDEN_ALGEBRA_NODES: frozenset[str] = frozenset(
    {
        "ServiceGraphPattern",  # SERVICE clause — federation / exfiltration
        # Update grammar nodes (defense in depth):
        "Load",
        "Clear",
        "Drop",
        "Create",
        "Add",
        "Move",
        "Copy",
        "InsertData",
        "DeleteData",
        "Modify",  # INSERT / DELETE / WHERE update form
    },
)

# Property-path classes from rdflib. Property paths are NOT CompValue
# nodes in the algebra tree — they appear as ``rdflib.paths.Path``
# subclasses in the predicate position of a triple. The depth check
# walks these instances directly rather than scanning the algebra tree
# for CompValue node names.
_PATH_OPERATOR_TYPES: tuple[type, ...] = (
    AlternativePath,
    SequencePath,
    InvPath,
    MulPath,
    NegatedPath,
)


class SparqlScrubError(ValueError):
    """Raised when a SPARQL ASK query violates one of the scrub rules.

    The message is user-facing — it appears in the form's validation
    error UI without further sanitisation. Keep it descriptive enough
    that the author can fix the query without reading the source.
    """


@dataclass(frozen=True)
class ScrubLimits:
    """Resolved limits for a single scrub invocation.

    The form layer reads limits from Django settings on each save; the
    engine layer does the same on each run. Bundling them in a small
    dataclass keeps the per-call signature short and makes the limits
    easy to override in tests without monkey-patching ``settings``.
    """

    max_query_length: int
    max_property_path_depth: int


def resolve_limits() -> ScrubLimits:
    """Read scrub limits from Django settings with sensible defaults.

    Returns a frozen :class:`ScrubLimits`. Settings keys:

    - ``SHACL_SPARQL_QUERY_LENGTH_MAX`` (default ``10_000``, hard cap
      ``50_000``)
    - ``SHACL_SPARQL_PROPERTY_PATH_DEPTH_MAX`` (default ``8``, hard cap
      ``32``)
    """
    return ScrubLimits(
        max_query_length=_positive_int_setting(
            "SHACL_SPARQL_QUERY_LENGTH_MAX",
            DEFAULT_MAX_QUERY_LENGTH,
            HARD_MAX_QUERY_LENGTH,
        ),
        max_property_path_depth=_positive_int_setting(
            "SHACL_SPARQL_PROPERTY_PATH_DEPTH_MAX",
            DEFAULT_MAX_PROPERTY_PATH_DEPTH,
            HARD_MAX_PROPERTY_PATH_DEPTH,
        ),
    )


def scrub_sparql_ask(
    query_text: str,
    *,
    limits: ScrubLimits | None = None,
) -> None:
    """Validate that ``query_text`` is a safe SPARQL ASK query.

    Returns ``None`` on success; raises :class:`SparqlScrubError` with a
    user-facing message on any policy violation. Called from both the
    form ``clean()`` (the primary line of defence) and the engine
    ``run_sparql_ask`` (belt-and-suspenders).

    Args:
        query_text: The raw SPARQL query text typed by the author.
        limits: Optional override for testing. When ``None``, limits
            are read from Django settings via :func:`resolve_limits`.

    Raises:
        SparqlScrubError: with a message naming the specific construct
            that violates policy. The message is safe to display
            verbatim to the author.
    """
    effective_limits = limits if limits is not None else resolve_limits()

    if query_text is None or not query_text.strip():
        msg = "SPARQL query is empty."
        raise SparqlScrubError(msg)

    # Cheap pre-check: refuse queries that exceed the length cap before
    # we hand them to the parser. Protects against pathologically large
    # inputs that could themselves be a DoS vector against rdflib's
    # parser.
    if len(query_text) > effective_limits.max_query_length:
        msg = (
            f"SPARQL query exceeds the maximum length of "
            f"{effective_limits.max_query_length:,} characters "
            f"(got {len(query_text):,}). Shorten the query or split it "
            "into multiple smaller assertions."
        )
        raise SparqlScrubError(msg)

    # Parse. rdflib's ``parseQuery`` refuses Update operations (INSERT,
    # DELETE, LOAD, etc.) outright — they belong to the separate Update
    # grammar accessed via ``parseUpdate``. A ``ParseException`` here is
    # most commonly a syntax error, which we surface with the original
    # message so the author can fix the query.
    try:
        parsed = parseQuery(query_text)
    except Exception as exc:
        msg = (
            "SPARQL syntax error: "
            f"{type(exc).__name__}: {exc}. "
            "Only SPARQL 1.1 ASK queries are supported."
        )
        raise SparqlScrubError(msg) from exc

    # ``parsed`` is a ``ParseResults`` of ``[prologue, query_body]``.
    # The query body is a ``CompValue`` whose ``.name`` is the grammar
    # production name. ASK queries produce ``AskQuery``.
    body = parsed[1] if len(parsed) > 1 else None
    if not isinstance(body, CompValue) or body.name != "AskQuery":
        actual = getattr(body, "name", type(body).__name__)
        msg = (
            f"Only SPARQL ASK queries are supported; got {actual}. "
            "SELECT, CONSTRUCT, and DESCRIBE are reserved for a future "
            "release; Update operations (INSERT, DELETE, LOAD, ...) are "
            "never permitted."
        )
        raise SparqlScrubError(msg)

    # Reject FROM / FROM NAMED before we translate to algebra.
    #
    # rdflib's ``CompValue`` has a quirky ``get()`` semantics: when the
    # key is missing it returns the key NAME (a string) rather than
    # ``None``. So we cannot use ``.get()`` here even though ruff SIM401
    # suggests it — that would silently turn a missing-key case into a
    # truthy string sentinel and trigger a false-positive rejection on
    # legitimate queries without FROM clauses.
    dataset_clauses = body["datasetClause"] if "datasetClause" in body else None  # noqa: SIM401
    if isinstance(dataset_clauses, (list, tuple)) and len(dataset_clauses) > 0:
        msg = (
            "SPARQL FROM and FROM NAMED clauses are not permitted. "
            "Queries run against the data / results / union graphs "
            "provided by the validator; no external graphs may be "
            "referenced."
        )
        raise SparqlScrubError(msg)

    # Translate to algebra. The algebra tree is where SERVICE and other
    # nested forbidden constructs surface as named ``CompValue`` nodes.
    try:
        translated = translateQuery(parsed)
    except Exception as exc:
        msg = (
            "SPARQL algebra translation failed: "
            f"{type(exc).__name__}: {exc}. "
            "The query is syntactically valid but the engine could not "
            "build an execution plan for it."
        )
        raise SparqlScrubError(msg) from exc

    # Walk the algebra. Raises ``SparqlScrubError`` on any forbidden
    # node or excessive property-path depth.
    _walk_algebra(
        translated.algebra,
        max_path_depth=effective_limits.max_property_path_depth,
    )


# =============================================================================
# Internal helpers
# =============================================================================


def _walk_algebra(node: Any, *, max_path_depth: int) -> None:
    """Recursively inspect the algebra tree for forbidden constructs.

    Walks every nested ``CompValue`` and every item inside a list/tuple
    payload. Raises :class:`SparqlScrubError` the moment it sees a
    forbidden node name or a property path that nests beyond the cap.

    Property paths are NOT CompValue subtypes — they are
    :class:`rdflib.paths.Path` instances that appear in the predicate
    position of a triple. We detect them with an ``isinstance`` check
    and recurse into their ``path`` / ``args`` attributes.
    """
    if isinstance(node, CompValue):
        name = node.name or ""
        if name in _FORBIDDEN_ALGEBRA_NODES:
            raise SparqlScrubError(_forbidden_node_message(name))
        for value in node.values():
            _walk_algebra(value, max_path_depth=max_path_depth)
        return

    if isinstance(node, _PATH_OPERATOR_TYPES):
        depth = _property_path_depth(node)
        if depth > max_path_depth:
            msg = (
                f"SPARQL property path nests {depth} levels deep, "
                f"over the limit of {max_path_depth}. Deeply nested "
                "paths can produce cubic-time evaluation on "
                "attacker-crafted graphs; rewrite the assertion "
                "with explicit triples or reduce path nesting."
            )
            raise SparqlScrubError(msg)
        return

    if isinstance(node, (list, tuple)):
        for item in node:
            _walk_algebra(item, max_path_depth=max_path_depth)


def _property_path_depth(path: Any, *, current: int = 0) -> int:
    """Return the maximum nesting depth of property-path operators.

    A leaf path (no further path operators inside) has depth 1; each
    nested path operator increments the depth. Plain triple predicates
    (URIRefs) and unwrapped paths are depth 0; only operator wrappers
    contribute. Recursive shape:

    - ``MulPath`` and ``InvPath`` have a ``.path`` attribute.
    - ``SequencePath`` and ``AlternativePath`` have an ``.args`` list.
    - ``NegatedPath`` has an ``.args`` list of one element.
    """
    if not isinstance(path, _PATH_OPERATOR_TYPES):
        return current

    deepest = current + 1

    inner = getattr(path, "path", None)
    if inner is not None and isinstance(inner, Path):
        deepest = max(deepest, _property_path_depth(inner, current=current + 1))

    args = getattr(path, "args", None)
    if isinstance(args, (list, tuple)):
        for arg in args:
            if isinstance(arg, Path):
                deepest = max(
                    deepest,
                    _property_path_depth(arg, current=current + 1),
                )

    return deepest


def _forbidden_node_message(name: str) -> str:
    """User-facing explanation of why a particular construct is banned."""
    if name == "ServiceGraphPattern":
        return (
            "SPARQL SERVICE clauses are not permitted. They federate "
            "queries to remote endpoints, which would let an assertion "
            "exfiltrate data from the validated graph to an external "
            "URL. Rewrite the assertion to query only the local graph."
        )
    if name in {"InsertData", "DeleteData", "Modify"}:
        return (
            f"SPARQL update operations are not permitted (got {name}). "
            "Only read-only ASK queries are supported."
        )
    if name == "Load":
        return (
            "SPARQL LOAD operations are not permitted. They fetch "
            "remote RDF documents over the network; the validator "
            "operates only on graphs provided by the pipeline."
        )
    return (
        f"SPARQL construct '{name}' is not permitted in author-defined "
        "ASK assertions. See ADR-2026-05-18 'Security' for the full "
        "rejection list."
    )


def _positive_int_setting(name: str, default: int, hard_max: int) -> int:
    """Read a positive integer Django setting and clamp it to a hard maximum."""
    try:
        value = int(getattr(django_settings, name, default))
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, hard_max)
