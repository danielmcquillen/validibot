"""Tests for the centralized CEL namespace-root allowlist.

Validibot assertions reference data through a small set of namespace prefixes
(``p``/``payload``, ``s``/``signal``, ``i``/``input``, ``o``/``output``,
``steps``). Historically the set of legal roots was hand-copied into five
places — the runtime context builder, ``RESERVED_CEL_NAMES``, the two
forms-layer allowlists, and the custom-validator rules view — and those copies
DRIFTED: the rules view silently omitted ``i``/``input``, so a perfectly valid
``i.<name>`` reference (which the runtime context binds) was rejected at
authoring time as a bare identifier.

These copies are now derived from one source of truth,
``validibot.validations.cel.CEL_NAMESPACE_ROOTS``. This suite locks that
arrangement in place so the drift cannot silently return:

* the reserved-signal-name set must still contain every namespace root
  (otherwise an author could name a signal ``payload`` and shadow it), and
* the custom-validator rules CEL validator must accept *all* namespace roots —
  most importantly ``i.``/``input.``, the regression that motivated the
  centralization.

Why this matters: a namespace prefix is the author's only door to the data a
rule inspects. A root that validates in one editor but is rejected in another
is the kind of inconsistency that makes the assertion language feel
untrustworthy, and it ships silently because each site looks correct in
isolation.
"""

from __future__ import annotations

import datetime

import pytest
from django.core.exceptions import ValidationError

from validibot.validations.cel import CEL_NAMESPACE_ROOTS
from validibot.validations.cel_eval import evaluate_cel_expression
from validibot.validations.constants import AssertionType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.models import RulesetAssertion
from validibot.validations.services.path_resolution import resolve_path
from validibot.validations.services.signal_resolution import RESERVED_CEL_NAMES
from validibot.validations.services.submission_context import (
    build_submission_assertion_context,
)
from validibot.validations.validators.basic import BasicValidator
from validibot.validations.views.rules import ValidatorRuleMixin

# ── The canonical set itself ────────────────────────────────────────────
# These tests pin the shape of the source of truth so a careless edit to
# CEL_NAMESPACE_ROOTS (dropping an alias, adding ``row`` globally) is caught.


def test_namespace_roots_contain_the_seven_namespaces_and_aliases():
    """The constant must hold all seven namespaces plus their short aliases.

    ``steps`` and ``submission`` are deliberately alias-free; the other five
    (``p``, ``s``, ``i``, ``o``, ``c``) each have a long-form alias for use in
    readable expressions. This is the contract every downstream allowlist
    relies on, so we assert it explicitly rather than trusting the literal to
    stay correct. ``c``/``const`` were added by ADR-2026-06-18 (Constants).
    """
    assert {
        "p",
        "payload",
        "s",
        "signal",
        "i",
        "input",
        "o",
        "output",
        "steps",
        "submission",
        "c",
        "const",
    } == CEL_NAMESPACE_ROOTS


def test_row_is_not_a_global_namespace_root():
    """``row`` must NOT live in the global constant.

    ``row.*`` is bound only by the Tabular Validator's row-stage loop, so it
    is added contextually by the tabular-aware allowlists. If it leaked into
    the global constant, a stray ``row.x`` on a JSON/XML step would be
    wrongly accepted.
    """
    assert "row" not in CEL_NAMESPACE_ROOTS


def test_submission_is_a_namespace_root():
    """The sixth namespace ``submission`` (ADR-2026-06-03b) is implemented.

    ``submission`` carries the submission envelope (submitter metadata +
    server-stamped facts) and is long-only — ``s`` already means ``signal``.
    Being in the constant is what flows it through every authoring-time
    allowlist; the runtime binding lives in ``_build_cel_context`` and is
    locked to this constant by the canary test in test_basic_validator.py.
    """
    assert "submission" in CEL_NAMESPACE_ROOTS
    # Long-only: there is no single-letter alias for submission.
    assert "sub" not in CEL_NAMESPACE_ROOTS


def test_constants_is_a_namespace_root():
    """The seventh namespace ``c`` / ``const`` (ADR-2026-06-18) is implemented.

    Constants are author-defined fixed literals from the workflow definition —
    the only namespace whose values are known at authoring time. Both the short
    ``c`` and long ``const`` spellings are roots (like ``s`` / ``signal``).
    Being in the constant is what flows them through every authoring-time
    allowlist and reserves them as signal/constant names; the runtime binding
    lives in ``_build_cel_context`` and is locked to this constant by the canary
    test in test_basic_validator.py.
    """
    assert "c" in CEL_NAMESPACE_ROOTS
    assert "const" in CEL_NAMESPACE_ROOTS


# ── Site 1: RESERVED_CEL_NAMES derives from the constant ─────────────────
# A signal must never be allowed to take a namespace root as its name, or it
# would shadow (or be shadowed by) that namespace at evaluation time.


def test_reserved_names_include_every_namespace_root():
    """Every namespace root must be a reserved signal name.

    ``RESERVED_CEL_NAMES`` is assembled by unioning ``CEL_NAMESPACE_ROOTS``
    with the CEL literals/built-ins and the Validibot helper names. This
    asserts the union actually happened — guarding against a future refactor
    that drops the roots and lets an author define a signal named ``output``.
    """
    assert CEL_NAMESPACE_ROOTS <= RESERVED_CEL_NAMES


# ── Site 4: the custom-validator rules CEL validator (the regression) ────
# This is the site that had drifted. Each test calls the validator directly;
# ``_validate_cel_expression`` only needs ``self._delimiters_balanced`` (a
# staticmethod), so the mixin can be exercised without a request or database.


@pytest.fixture
def rule_validator():
    """A bare ``ValidatorRuleMixin`` instance for direct method calls.

    The mixin defines no custom ``__init__`` and the validator method under
    test reads no request/DB state, so a plain instance is sufficient and
    keeps the test a fast, isolated unit test.
    """
    return ValidatorRuleMixin()


@pytest.mark.parametrize(
    "expr",
    [
        "i.threshold > 0",
        "input.threshold > 0",
        "i.threshold > 0 && input.other < 10",
    ],
)
def test_rules_validator_accepts_input_namespace(rule_validator, expr):
    """``i.``/``input.`` references must validate in the rules editor.

    This is the regression. Before the allowlist was centralized, the rules
    view's namespace set omitted ``i``/``input``, so these expressions raised
    "Bare identifiers are not allowed" even though the runtime context binds
    the ``i.*`` namespace. Passing an empty ``available_entries`` is
    intentional: the namespace root must be accepted regardless of whether a
    matching signal definition exists (membership only affects target
    tracking, not legality).
    """
    # Must not raise. The return value (referenced signal definitions) is
    # empty here because inputs are never tracked as assertion targets.
    assert rule_validator._validate_cel_expression(expr, []) == []


@pytest.mark.parametrize(
    "root",
    sorted(CEL_NAMESPACE_ROOTS),
)
def test_rules_validator_accepts_every_namespace_root(rule_validator, root):
    """Every canonical namespace root must be accepted by the rules editor.

    Driving the parametrization from ``CEL_NAMESPACE_ROOTS`` means that when a
    namespace is eventually added to the constant, this test automatically
    demands the rules validator accept it too — exactly the lockstep the
    centralization is meant to guarantee. ``steps`` needs an extra path
    segment to read as a reference rather than a bare word.
    """
    expr = f"{root}.value == 1" if root != "steps" else "steps.a.output.value == 1"
    # Must not raise for any legal root.
    rule_validator._validate_cel_expression(expr, [])


def test_rules_validator_still_rejects_unknown_bare_identifiers(rule_validator):
    """A non-namespace identifier must still be rejected.

    Centralizing the allowlist must not loosen it: an identifier whose root
    is not a known namespace (here ``bogus``) is still a bare identifier and
    must raise, preserving the "namespaced references only" contract.
    """
    with pytest.raises(ValidationError):
        rule_validator._validate_cel_expression("bogus.value > 0", [])


# ── The submission envelope builder (ADR-2026-06-03b) ────────────────────
# build_submission_assertion_context is the single source of the envelope.
# Duck-typed fakes keep these as fast unit tests — the builder reads only via
# getattr, so no model instances or database are needed.


class _FakeSubmission:
    """A minimal stand-in for ``Submission`` for builder unit tests."""

    name = "model.ttl"
    original_filename = "model.ttl"
    file_type = "ttl"
    size_bytes = 2048
    metadata = {"deliverable": "handover", "phase 2": "wip"}
    created = datetime.datetime(2026, 6, 3, 12, 0, 0, tzinfo=datetime.UTC)


_UNSET = object()


class _FakeRun:
    """A minimal stand-in for ``ValidationRun`` (note: short_description is a
    run field, not a submission field — the builder must read it from here)."""

    def __init__(self, submission=_UNSET):
        # Sentinel default so callers can pass ``submission=None`` explicitly to
        # exercise the no-submission path, while the common case gets a fake.
        self.submission = _FakeSubmission() if submission is _UNSET else submission
        self.short_description = "Final handover package"


def test_builder_assembles_full_envelope():
    """The builder must surface every contract field from the right source.

    This pins the contract table: submitter-set fields (name, metadata,
    original_filename) come from the Submission; short_description comes from
    the RUN; server facts (file_type, size→size_bytes, uploaded_at→created)
    come from the Submission. A field read from the wrong object is the kind
    of mistake that makes a gate silently read null.
    """
    env = build_submission_assertion_context(_FakeRun())
    assert env["name"] == "model.ttl"
    assert env["short_description"] == "Final handover package"
    assert env["metadata"]["deliverable"] == "handover"
    assert env["original_filename"] == "model.ttl"
    assert env["file_type"] == "ttl"
    assert env["size"] == 2048  # noqa: PLR2004 — the fixture's byte count
    assert env["uploaded_at"] == _FakeSubmission.created


def test_builder_returns_empty_envelope_when_no_run_or_submission():
    """A null run or a run without a submission yields ``{}``, never a raise.

    This matches how missing signals behave and keeps ``submission.*`` safe to
    reference even in unit contexts (``_build_cel_context`` called without a
    run) and for runs whose submission was never attached.
    """
    assert build_submission_assertion_context(None) == {}
    assert build_submission_assertion_context(_FakeRun(submission=None)) == {}


# ── submission.* resolves end-to-end through CEL ─────────────────────────
# These prove the namespace actually evaluates, including the file-type
# independence (it works against an empty payload, the .ttl case) and the
# server-stamped timestamp (the trustworthy freshness fact).


@pytest.fixture
def submission_context():
    """A CEL context carrying a populated ``submission`` envelope.

    Built directly from the shared builder (the same call ``_build_cel_context``
    makes) so the test exercises the real envelope shape.
    """
    return {"submission": build_submission_assertion_context(_FakeRun())}


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        # Nested metadata via dot notation (identifier-safe key).
        ('submission.metadata.deliverable == "handover"', True),
        # Server fact: size is an integer count of bytes.
        ("submission.size > 1000", True),
        # Server fact: file_type is the enum string.
        ('submission.file_type == "ttl"', True),
        # Submitter-set scalar.
        ('submission.name == "model.ttl"', True),
        # Non-identifier metadata key MUST use bracket notation (celpy native).
        ('submission.metadata["phase 2"] == "wip"', True),
    ],
)
def test_submission_namespace_evaluates_in_cel(submission_context, expr, expected):
    """``submission.*`` references must evaluate correctly in CEL.

    Covers the field types the contract promises (string, int, nested map) and
    both metadata key spellings — dot for identifier-safe keys, brackets for
    free-form keys like ``"phase 2"``. This is the read side that makes the
    deliverable-phase acceptance gate possible.
    """
    result = evaluate_cel_expression(expression=expr, context=submission_context)
    assert result.success, result.error
    assert result.value == expected


def test_submission_uploaded_at_is_a_cel_timestamp(submission_context):
    """``submission.uploaded_at`` must arrive as a comparable CEL timestamp.

    The whole point of the server-stamped fact is a trustworthy freshness
    rule. ``celpy.json_to_cel`` converts the Python ``datetime`` to a CEL
    ``timestamp``, so it compares against ``timestamp(...)`` / ``now()`` and
    supports ``duration`` arithmetic. We pin ``now()`` to keep the run
    deterministic, exactly as a real evaluation does.
    """
    pinned_now = datetime.datetime(2026, 6, 4, 12, 0, 0, tzinfo=datetime.UTC)
    # Uploaded 2026-06-03; "within 30 days" of the pinned now must hold.
    result = evaluate_cel_expression(
        expression='now() - submission.uploaded_at < duration("720h")',
        context=submission_context,
        now=pinned_now,
    )
    assert result.success, result.error
    # celpy returns a BoolType (an int subclass), not the Python singleton.
    assert bool(result.value) is True


def test_submission_namespace_is_file_type_independent():
    """``submission.*`` must resolve even when the payload is empty.

    This is the ADR's headline property. For a non-JSON submission (RDF
    ``.ttl``/SHACL) the file content cannot be parsed into ``p``/``s``, so for
    that workflow ``p`` is effectively empty — yet the envelope lives BESIDE
    the file and must still carry a per-submission gate value. We model that
    with an empty payload and assert ``submission.metadata.*`` still reads.
    """
    context = {
        "p": {},
        "payload": {},
        "submission": build_submission_assertion_context(_FakeRun()),
    }
    result = evaluate_cel_expression(
        expression='submission.metadata.deliverable == "handover"',
        context=context,
    )
    assert result.success, result.error
    # celpy returns a BoolType (an int subclass), not the Python singleton.
    assert bool(result.value) is True


# ── submission.* in the BASIC (non-CEL) assertion path ───────────────────
# Basic assertions don't go through the CEL evaluator; they walk a dotted
# path against an enriched payload. The envelope is injected as a NESTED
# ``submission`` sub-dict so resolve_path reaches it the same way CEL does.


class _FakeRunContext:
    """Minimal run context exposing what ``_enrich_basic_payload`` reads."""

    def __init__(self, run):
        self.validation_run = run
        self.workflow_signals: dict = {}


def _basic_validator_with_submission():
    """A BasicValidator whose run context carries a populated submission."""
    validator = BasicValidator()
    validator.run_context = _FakeRunContext(_FakeRun())
    return validator


def test_basic_payload_injects_nested_submission_dict():
    """A dict payload gets the envelope as a nested ``submission`` sub-dict.

    The basic evaluator walks a dotted target against the payload, so the
    envelope must be reachable at ``payload["submission"][...]`` — nested, not
    flattened to composite keys — for ``submission.metadata.deliverable`` to
    resolve identically to CEL.
    """
    validator = _basic_validator_with_submission()
    enriched = validator._enrich_basic_payload(
        {"building": {"area": 100}},
        stage="input",
    )
    assert enriched["building"] == {"area": 100}
    assert enriched["submission"]["metadata"]["deliverable"] == "handover"
    # And the target a basic assertion would store resolves against it.
    val, found = resolve_path(enriched, "submission.metadata.deliverable")
    assert found is True
    assert val == "handover"


def test_basic_payload_injects_submission_for_non_dict_payload():
    """A NON-dict payload (RDF/.ttl/SHACL) still exposes the envelope.

    This is the ADR's headline property on the basic side. The method used to
    return a non-dict payload unchanged, so a metadata-only basic assertion on
    a ``.ttl`` submission saw nothing. It must instead return a minimal dict
    carrying the injectable namespaces so ``submission.*`` resolves; ``p.*``
    stays unavailable (the raw graph isn't walkable).
    """
    validator = _basic_validator_with_submission()
    # Simulate a parsed non-dict payload (e.g. an RDF graph object).
    enriched = validator._enrich_basic_payload("<rdf-graph-object>", stage="output")
    assert isinstance(enriched, dict)
    val, found = resolve_path(enriched, 'submission.metadata["phase 2"]')
    assert found is True
    assert val == "wip"


def test_basic_submission_is_authoritative_over_payload_key():
    """An injected ``submission`` overrides a same-named payload key.

    ``submission`` is a reserved namespace, so a ``submission.*`` basic target
    must read the envelope — never a file that happens to have a top-level
    ``submission`` key. (CEL avoids this entirely by keeping ``submission`` a
    separate top-level context key; the flattened basic payload needs the
    envelope to win on collision.)
    """
    validator = _basic_validator_with_submission()
    enriched = validator._enrich_basic_payload(
        {"submission": {"metadata": {"deliverable": "FAKE"}}},
        stage="input",
    )
    assert enriched["submission"]["metadata"]["deliverable"] == "handover"


def test_basic_payload_merges_bound_and_parser_input_values(monkeypatch):
    """Payload enrichment must not discard parser-derived canonical inputs.

    Advanced validators may combine implicit parser facts with explicit author
    bindings. Updating the BASIC assertion view must preserve both sources on
    the run context so later persistence and assertion paths see one contract.
    """
    validator = _basic_validator_with_submission()
    validator.run_context.step_input_contract_values = {"zone_count": 3}
    monkeypatch.setattr(
        validator,
        "_resolve_bound_input_context",
        lambda _payload: {"weather_file": "melbourne.epw"},
    )

    validator._enrich_basic_payload({"building": {}}, stage="input")

    assert validator.run_context.step_input_contract_values == {
        "zone_count": 3,
        "weather_file": "melbourne.epw",
    }


# ── Stage classification (RulesetAssertion.resolved_run_stage) ───────────
# The envelope is fixed at submission time, so a submission-only assertion is
# an early INPUT-stage gate; one that also needs outputs stays OUTPUT. These
# build RulesetAssertion in memory (no save) — resolved_run_stage reads only
# the type, target path, and rhs, so no database is touched.


def _basic_assertion(path):
    return RulesetAssertion(
        assertion_type=AssertionType.BASIC,
        target_data_path=path,
        rhs={},
    )


def _cel_assertion(expr):
    return RulesetAssertion(
        assertion_type=AssertionType.CEL_EXPRESSION,
        target_data_path="",
        rhs={"expr": expr},
    )


def test_basic_submission_target_classifies_input():
    """A basic ``submission.*`` target is an INPUT-stage gate.

    The envelope is knowable before the validator runs, so an early gate like
    "reject unless submission.metadata.deliverable == 'handover'" can fire
    before any (possibly expensive) container dispatch.
    """
    assert (
        _basic_assertion("submission.metadata.deliverable").resolved_run_stage
        == CatalogRunStage.INPUT
    )


def test_cel_submission_only_classifies_input():
    """A CEL expression reading only ``submission.*`` is INPUT-stage."""
    assert (
        _cel_assertion(
            'submission.metadata.deliverable == "handover"'
        ).resolved_run_stage
        == CatalogRunStage.INPUT
    )


def test_cel_submission_plus_output_stays_output():
    """A CEL expression that also reads ``o.*`` stays OUTPUT-stage.

    ``submission.metadata.expected_zones == o.zone_count`` genuinely needs
    results, so it must run at output stage even though it also reads the
    (always-available) envelope. This is the carve-out that keeps the
    one-stage-per-assertion model intact.
    """
    assert (
        _cel_assertion(
            "submission.metadata.expected_zones == o.zone_count"
        ).resolved_run_stage
        == CatalogRunStage.OUTPUT
    )


def test_non_submission_assertions_are_unaffected():
    """Adding submission classification must not move existing assertions.

    A plain ``o.*`` target and a CEL expression that mentions neither
    submission nor i.* keep their legacy OUTPUT classification.
    """
    assert _basic_assertion("o.zone_count").resolved_run_stage == CatalogRunStage.OUTPUT
    assert _cel_assertion("o.result > 0").resolved_run_stage == CatalogRunStage.OUTPUT
