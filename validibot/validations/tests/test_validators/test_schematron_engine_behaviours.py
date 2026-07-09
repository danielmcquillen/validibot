"""Schematron engine-behaviour matrix (ADR-2026-07-01 test layer A).

Where ``test_schematron_fixture_helper.py`` proves the *one* flagship invoice
scenario round-trips, this suite is the **behaviour matrix**: many small,
novel ``.sch`` + XML combinations, each isolating a single Schematron mechanic
so a regression points at exactly one thing. It runs them through
``lxml.isoschematron`` — the XSLT-1.0 **fixture-generating helper**, never a
production runtime (the container's Saxon path is layer C, in
``validibot-validator-backends``) — and parses the resulting SVRL with the
canonical shared parser. What it guards is the contract every layer shares:
the SVRL a real engine emits maps to the findings, severities and signals
``parse_svrl`` promises.

Deliberately does NOT instantiate ``SchematronValidator``: that class is an
``AdvancedValidator`` that dispatches to a container and must never run an XSLT
engine in-process (D4's "one execution path"). Keeping lxml out of the
validator and in the fixtures is what enforces that separation in the suite.

The matrix covers four axes (all authored here, none copied from any external
rule set):

1. **Rule / pattern semantics** — ``assert`` vs ``report``, first-match-wins
   rule shadowing, abstract patterns (``is-a``) and abstract rules
   (``extends``), ``let`` variables, and ``phase`` selection.
2. **Messages & severity** — the ``@flag`` -> ``@role`` -> fail-closed-ERROR
   chain (D3), ``value-of`` interpolation, and the ``info``/``warning``/``fatal``
   spread.
3. **Namespaces & the false-green guard** — multi-namespace resolution via
   ``ns`` bindings, and the "no rule context matched" case that
   ``fired_rule_count`` exists to catch.
4. **Failure & edge paths** — malformed / non-Schematron / bad-XPath sources,
   the volume cap and its ERROR-first ordering, duplicate-id severity
   collapse, unicode, and empty SVRL.

Some cases (duplicate-id collapse, truncation ordering, empty SVRL) exercise
``parse_svrl`` against crafted SVRL because lxml cannot *produce* the input —
e.g. libxml2's RELAX NG validation rejects a ``.sch`` with duplicate ``@id``
values at compile time, so the only way a duplicate id reaches the parser is
from a different engine's report. Each such case says so in its docstring.

Skips as a module until validibot-shared ships the canonical SVRL parser
(>= 0.11.0); activates automatically once the released package is synced.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree
from lxml import isoschematron

# The SVRL parser is canonical in validibot-shared (>= 0.11.0); the community
# module re-exports it. Skip until the released package is synced.
svrl = pytest.importorskip(
    "validibot.validations.validators.schematron.svrl",
    reason="requires validibot-shared >= 0.11.0 (canonical SVRL parser)",
)
parse_svrl = svrl.parse_svrl
SvrlParseError = svrl.SvrlParseError
SEVERITY_ERROR = svrl.SEVERITY_ERROR
SEVERITY_WARNING = svrl.SEVERITY_WARNING
SEVERITY_INFO = svrl.SEVERITY_INFO

ASSETS = Path("tests/assets/schematron")
PO = ASSETS / "purchase_order"

SVRL_NS = "http://purl.oclc.org/dsdl/svrl"


def _run(
    sch_text: str, xml_text: str, *, phase: str | None = None, max_findings: int = 500
):
    """Compile ``sch_text`` via lxml, validate ``xml_text``, parse the SVRL.

    Returns the ``SvrlSummary`` our parser produces from the report — the
    single object every assertion in this file reasons about. ``phase`` selects
    a Schematron phase (a named subset of patterns); ``max_findings`` drives the
    volume cap.
    """
    kwargs = {"store_report": True}
    if phase is not None:
        kwargs["phase"] = phase
    schematron = isoschematron.Schematron(
        etree.fromstring(sch_text.encode("utf-8")),
        **kwargs,
    )
    schematron.validate(etree.ElementTree(etree.fromstring(xml_text.encode("utf-8"))))
    return parse_svrl(
        etree.tostring(schematron.validation_report), max_findings=max_findings
    )


def _run_pack(sch_filename: str, xml_filename: str):
    """Compile an on-disk ``.sch`` and validate an on-disk XML fixture."""
    schematron = isoschematron.Schematron(
        etree.parse(str(PO / sch_filename)),
        store_report=True,
    )
    schematron.validate(etree.parse(str(PO / xml_filename)))
    return parse_svrl(etree.tostring(schematron.validation_report))


# The Schematron namespace, repeated by every inline fixture below.
_SCH = 'xmlns="http://purl.oclc.org/dsdl/schematron"'

# Named expected counts. The project lints bare magic values in comparisons
# (PLR2004), so each number is given a meaning at its use site.
_SHADOWED_ITEM_CONTEXTS = 2  # two items, each firing exactly one rule
_OVERFLOW_FINDINGS = 12  # failures produced before the volume cap applies
_FINDINGS_CAP = 5  # max_findings used in the truncation test
_SUPPRESSED_FINDINGS = _OVERFLOW_FINDINGS - _FINDINGS_CAP  # 7
_DUPLICATE_ID_ROWS = 2  # both occurrences of a duplicate id are kept
_PO_RULE_CONTEXTS = 4  # order + two lines + audit all match
_PO_WARNING_COUNT = 2  # deprecated-status report + missing description


# ── Rule / pattern semantics: assert vs report ───────────────────────────────
# ``assert`` fires when its test is FALSE (a failed-assert); ``report`` fires
# when its test is TRUE (a successful-report). Both are ACTIVE findings — a
# publisher can put an error in a report — so the element type must never drive
# severity or pass/fail. These two tests pin that symmetry.


def test_assert_fires_when_its_test_is_false():
    """A failed assertion becomes a finding; a satisfied one is silent.

    The everyday case: ``assert test="@sku"`` on an item with no ``@sku``
    yields exactly one finding carrying the assertion's id and message.
    """
    summary = _run(
        f'<schema {_SCH}><pattern><rule context="item">'
        '<assert test="@sku" flag="fatal" id="A-SKU">item needs a sku.</assert>'
        "</rule></pattern></schema>",
        "<item/>",
    )

    assert summary.error_count == 1
    assert summary.findings[0].rule_id == "A-SKU"
    assert summary.findings[0].element == "failed-assert"
    assert not summary.passed


def test_report_fires_when_its_test_is_true_as_a_successful_report():
    """A ``report`` is the inverse of ``assert`` and still surfaces a finding.

    ``report test="@deprecated='true'"`` fires *because* the condition holds.
    parse_svrl must treat the resulting ``successful-report`` as an active
    finding (here a WARNING via ``@flag``), not discard it as "the rule
    passed" — the exact trap the D3 "both element types are findings" rule
    guards against.
    """
    summary = _run(
        f'<schema {_SCH}><pattern><rule context="widget">'
        '<report test="@deprecated=\'true\'" flag="warning" id="R-DEP-01">'
        "Widget is deprecated.</report>"
        "</rule></pattern></schema>",
        "<widget deprecated='true'/>",
    )

    assert summary.warning_count == 1
    assert summary.error_count == 0
    finding = summary.findings[0]
    assert finding.rule_id == "R-DEP-01"
    assert finding.element == "successful-report"
    assert finding.severity == SEVERITY_WARNING
    # No ERROR findings -> the run passes even though a report fired.
    assert summary.passed


# ── Messages & severity: the @flag -> @role -> fail-closed chain (D3) ─────────


def test_flag_maps_fatal_warning_and_info_to_distinct_severities():
    """One document, three flags, three severities — the full spread.

    ``fatal`` -> ERROR, ``warning`` -> WARNING, ``info`` -> INFO. Getting this
    wrong would collapse an actionable error and an advisory note into the same
    bucket, which is the whole reason severities exist.
    """
    summary = _run(
        f'<schema {_SCH}><pattern><rule context="item">'
        '<assert test="@sku" flag="fatal" id="A-FATAL">no sku</assert>'
        '<assert test="@price" flag="warning" id="A-WARN">no price</assert>'
        '<assert test="@desc" flag="info" id="A-INFO">no desc</assert>'
        "</rule></pattern></schema>",
        "<item/>",
    )

    assert (summary.error_count, summary.warning_count, summary.info_count) == (1, 1, 1)
    assert summary.finding_rule_ids_by_severity == {
        "A-FATAL": SEVERITY_ERROR,
        "A-WARN": SEVERITY_WARNING,
        "A-INFO": SEVERITY_INFO,
    }
    assert not summary.passed


def test_role_drives_severity_when_flag_is_absent():
    """With no ``@flag``, severity falls back to ``@role`` (step 2 of the chain).

    ``role="warning"`` with no flag yields a WARNING, so the run PASSES (no
    ERROR) even though an assertion failed — proving the fallback, and that a
    warning alone never fails a run.
    """
    summary = _run(
        f'<schema {_SCH}><pattern><rule context="item">'
        '<assert test="@sku" role="warning" id="A-ROLE">no sku</assert>'
        "</rule></pattern></schema>",
        "<item/>",
    )

    assert summary.warning_count == 1
    assert summary.findings[0].severity == SEVERITY_WARNING
    assert summary.findings[0].role == "warning"
    assert summary.passed


def test_missing_flag_and_role_fails_closed_to_error():
    """A finding with neither ``@flag`` nor ``@role`` fail-closes to ERROR (D3).

    Nothing publisher-authored is silently downgraded: an unclassified finding
    is treated as the most severe, never dropped. This is the safety end of the
    resolution chain.
    """
    summary = _run(
        f'<schema {_SCH}><pattern><rule context="x">'
        '<assert test="false()" id="A-NOATTR">no flag, no role</assert>'
        "</rule></pattern></schema>",
        "<x/>",
    )

    assert summary.error_count == 1
    finding = summary.findings[0]
    assert finding.severity == SEVERITY_ERROR
    assert finding.flag == ""
    assert finding.role == ""


def test_value_of_interpolates_document_data_into_the_message():
    """``value-of`` pulls live document values into the assertion text.

    A finding is only actionable if it can quote the offending value, so the
    parser must preserve the interpolated ``svrl:text`` verbatim — here the
    observed qty (3) appears in the human message.
    """
    summary = _run(
        f'<schema {_SCH}><pattern><rule context="order">'
        '<assert test="number(@qty) &gt;= 10" flag="fatal" id="A-VOF">'
        'qty <value-of select="@qty"/> is below the minimum of 10.</assert>'
        "</rule></pattern></schema>",
        "<order qty='3'/>",
    )

    assert summary.findings[0].message == "qty 3 is below the minimum of 10."


# ── Rule semantics: let variables, first-match-wins, abstract patterns ───────


def test_let_variables_are_available_globally_and_per_rule():
    """``let`` binds reusable values; both scopes resolve in a test.

    A global ``let`` (the minimum) and a rule-local ``let`` (the observed qty)
    are both referenced in one assertion — proving variable resolution works so
    packs can factor out magic numbers instead of repeating them.
    """
    summary = _run(
        f"<schema {_SCH}>"
        '<let name="minqty" value="10"/>'
        '<pattern><rule context="order">'
        '<let name="q" value="number(@qty)"/>'
        '<assert test="$q &gt;= $minqty" flag="fatal" id="A-LET">'
        "qty below minimum.</assert>"
        "</rule></pattern></schema>",
        "<order qty='3'/>",
    )

    assert summary.error_count == 1
    assert summary.findings[0].rule_id == "A-LET"


def test_first_match_wins_shadows_later_rules_in_a_pattern():
    """A node matched by an earlier rule is excluded from later rules (D-order).

    Schematron's most surprising semantic: within one pattern, each node is
    processed by the FIRST rule whose context matches, and no other. Here a
    specific ``item[@type='special']`` rule precedes a generic ``item`` rule;
    the special item fires ONLY the specific rule, the plain item ONLY the
    generic one. If shadowing broke, the special item would report both.
    """
    summary = _run(
        f"<schema {_SCH}><pattern>"
        "<rule context=\"item[@type='special']\">"
        '<assert test="false()" flag="fatal" id="A-SPECIAL">special fired.</assert>'
        "</rule>"
        '<rule context="item">'
        '<assert test="false()" flag="fatal" id="A-GENERIC">generic fired.</assert>'
        "</rule>"
        "</pattern></schema>",
        "<root><item type='special'/><item/></root>",
    )

    # Two contexts evaluated (one per item), each firing exactly one rule.
    assert summary.fired_rule_count == _SHADOWED_ITEM_CONTEXTS
    fired = {f.rule_id for f in summary.findings}
    assert fired == {"A-SPECIAL", "A-GENERIC"}
    special = next(f for f in summary.findings if f.rule_id == "A-SPECIAL")
    generic = next(f for f in summary.findings if f.rule_id == "A-GENERIC")
    # The special item (document position 1) never reached the generic rule.
    assert special.location.endswith("item[1]")
    assert generic.location.endswith("item[2]")


def test_abstract_pattern_is_a_substitutes_its_parameter():
    """An abstract pattern is reused via ``is-a`` with a ``param`` binding.

    The abstract pattern asserts non-negativity against a ``$ctx`` placeholder;
    the concrete pattern binds ``ctx`` to ``account/balance``. This is how packs
    apply one rule shape at many locations without copy-paste, and the finding
    must land at the substituted context.
    """
    summary = _run(
        f"<schema {_SCH}>"
        '<pattern abstract="true" id="p-nonneg"><rule context="$ctx">'
        '<assert test="number(.) &gt;= 0" flag="fatal" id="A-NONNEG">'
        "value must be non-negative.</assert>"
        "</rule></pattern>"
        '<pattern is-a="p-nonneg" id="p-balance">'
        '<param name="ctx" value="account/balance"/>'
        "</pattern></schema>",
        "<account><balance>-5</balance></account>",
    )

    assert summary.error_count == 1
    assert summary.findings[0].rule_id == "A-NONNEG"
    assert summary.findings[0].location.endswith("balance")


def test_abstract_rule_extends_shares_common_assertions():
    """A concrete rule pulls in an abstract rule's assertions via ``extends``.

    The abstract rule contributes a shared "must have id" check; the concrete
    rule adds its own "should have name" warning. Both fire on the target node,
    proving ``extends`` composition — the rule-level counterpart to ``is-a``.
    """
    summary = _run(
        f"<schema {_SCH}><pattern>"
        '<rule abstract="true" id="common">'
        '<assert test="@id" flag="fatal" id="A-HASID">must have id.</assert>'
        "</rule>"
        '<rule context="node">'
        '<extends rule="common"/>'
        '<assert test="@name" flag="warning" id="A-HASNAME">should have name.</assert>'
        "</rule></pattern></schema>",
        "<node/>",
    )

    assert summary.finding_rule_ids_by_severity == {
        "A-HASID": SEVERITY_ERROR,
        "A-HASNAME": SEVERITY_WARNING,
    }


# ── Rule semantics: phases select a subset of patterns ───────────────────────
# A phase names a group of patterns to activate; the same schema validates
# differently depending on which phase is selected. (In production the phase is
# declared by the schema's ``defaultPhase``; here we drive it explicitly to
# assert each subset in isolation.)

_PHASE_SCH = (
    f"<schema {_SCH}>"
    '<phase id="structure"><active pattern="p-struct"/></phase>'
    '<phase id="business"><active pattern="p-biz"/></phase>'
    '<pattern id="p-struct"><rule context="invoice">'
    '<assert test="line" flag="fatal" id="A-HASLINE">need a line.</assert>'
    "</rule></pattern>"
    '<pattern id="p-biz"><rule context="invoice">'
    '<assert test="number(@total) &gt; 0" flag="fatal" id="A-POSTOTAL">'
    "total must be positive.</assert>"
    "</rule></pattern></schema>"
)


def test_phase_structure_activates_only_the_structural_pattern():
    """Selecting the ``structure`` phase runs only its pattern.

    The document has a line but a zero total; under ``structure`` only the line
    rule is active, so it passes — the business rule is not evaluated at all.
    """
    summary = _run(
        _PHASE_SCH, "<invoice total='0'><line/></invoice>", phase="structure"
    )

    assert summary.finding_rule_ids_by_severity == {}
    assert summary.passed


def test_phase_business_activates_only_the_business_pattern():
    """Selecting the ``business`` phase runs only its pattern.

    Same document; under ``business`` the positive-total rule fires and the
    structural rule is skipped — proving phase selection genuinely partitions
    the schema rather than always running everything.
    """
    summary = _run(_PHASE_SCH, "<invoice total='0'><line/></invoice>", phase="business")

    assert summary.finding_rule_ids_by_severity == {"A-POSTOTAL": SEVERITY_ERROR}


def test_phase_all_activates_every_pattern():
    """The ``#ALL`` phase runs every pattern (the default-everything case).

    A document that violates BOTH rules yields both findings, confirming that
    the per-phase results above were genuine subsets of this whole.
    """
    summary = _run(_PHASE_SCH, "<invoice total='0'/>", phase="#ALL")

    assert set(summary.finding_rule_ids_by_severity) == {"A-HASLINE", "A-POSTOTAL"}


# ── Namespaces & the false-green guard ───────────────────────────────────────


def test_multi_namespace_resolution_via_ns_bindings():
    """Prefix bindings in the schema resolve elements in different namespaces.

    A rule bound to ``a:root`` asserts the presence of a ``b:meta`` child, where
    ``a`` and ``b`` are distinct namespaces declared by ``ns``. The passing doc
    supplies ``b:meta`` (fires, no findings); the failing doc omits it. Getting
    a binding wrong is a classic silent-failure source, so this pins both ends.
    """
    schema = (
        f"<schema {_SCH}>"
        '<ns prefix="a" uri="urn:example:a"/>'
        '<ns prefix="b" uri="urn:example:b"/>'
        '<pattern><rule context="/a:root">'
        '<assert test="b:meta" flag="fatal" id="A-NEEDMETA">'
        "root must contain a b:meta child.</assert>"
        "</rule></pattern></schema>"
    )

    ok = _run(
        schema, "<root xmlns='urn:example:a' xmlns:b='urn:example:b'><b:meta/></root>"
    )
    assert ok.fired_rule_count == 1
    assert ok.passed

    missing = _run(schema, "<root xmlns='urn:example:a'/>")
    assert missing.error_count == 1
    assert missing.findings[0].rule_id == "A-NEEDMETA"


def test_no_matching_context_is_a_false_green_that_fired_rule_count_catches():
    """Zero matched contexts = "checked nothing", not "everything is fine".

    The schema binds ``x`` to one namespace but the document uses another, so
    ``/x:root/x:widget`` matches nothing: zero findings AND ``passed`` True.
    Only ``fired_rule_count == 0`` distinguishes this false green from a real
    pass — which is exactly why the signal exists and why gates should check it.
    """
    summary = _run(
        f"<schema {_SCH}>"
        '<ns prefix="x" uri="urn:example:x"/>'
        '<pattern><rule context="/x:root/x:widget">'
        '<assert test="@ok" flag="fatal" id="A-NEVER">never reached.</assert>'
        "</rule></pattern></schema>",
        "<root xmlns='urn:example:OTHER'><widget/></root>",
    )

    assert summary.fired_rule_count == 0
    assert summary.findings == []
    assert summary.passed  # ... but nothing was actually validated.


# ── Failure & edge paths: unsafe / broken sources ────────────────────────────
# Every one of these is a "the rules could not run" condition. In production the
# container maps them to the reserved ``schematron.rules_invalid`` finding (D9);
# here we prove the fixture engine raises rather than silently passing, and that
# each failure mode has a distinct, catchable signature.


def test_malformed_schematron_raises_at_compile_time():
    """A ``.sch`` that is not well-formed XML fails when compiled, not at run.

    Catches the authoring mistake early: the parse blows up before any document
    is validated, so a broken rules upload can never masquerade as "0 findings".
    """
    with pytest.raises(etree.XMLSyntaxError):
        isoschematron.Schematron(
            etree.fromstring(f'<schema {_SCH}><pattern><rule context="x">'.encode()),
        )


def test_non_schematron_document_is_rejected():
    """A well-formed XML document that is not Schematron is refused.

    Uploading, say, an XSD or a plain XML file where rules were expected must be
    a clear rejection, not a run that quietly validates nothing.
    """
    with pytest.raises(etree.SchematronParseError):
        isoschematron.Schematron(
            etree.fromstring(b'<schema xmlns="urn:not:schematron"><pattern/></schema>'),
        )


def test_duplicate_rule_ids_in_source_are_rejected_at_compile_time():
    """A schema reusing an ``@id`` on two assertions will not compile under lxml.

    libxml2's RELAX NG validation of the Schematron treats ``@id`` as an
    XML ID and enforces uniqueness, so duplicate ids are a compile error. This
    is why the duplicate-id *collapse* behaviour below is exercised against
    crafted SVRL — lxml cannot emit a report containing a repeated id.
    """
    with pytest.raises(etree.SchematronParseError):
        isoschematron.Schematron(
            etree.fromstring(
                f"<schema {_SCH}><pattern>"
                '<rule context="a"><assert test="false()" id="DUP">a</assert></rule>'
                '<rule context="b"><assert test="false()" id="DUP">b</assert></rule>'
                "</pattern></schema>".encode(),
            ),
        )


# ── Failure & edge paths: parse_svrl robustness on crafted / capped input ────
# These reason about the parser directly. Some inputs (duplicate ids, a mixed
# severity overflow) are easier — or only possible — to construct as SVRL by
# hand than to coax a specific engine into emitting.


def test_findings_are_volume_capped_with_an_explicit_suppressed_count():
    """The parser caps the findings list but keeps the counts truthful (D10).

    A document with twelve identical failures, parsed with a cap of five, keeps
    five findings, flags truncation, and records seven suppressed — while
    ``error_count`` still reports the true twelve. "Clean-ish" and "hundreds of
    errors, capped" must never look the same.
    """
    items = "".join(f"<item n='{i}'/>" for i in range(_OVERFLOW_FINDINGS))
    summary = _run(
        f'<schema {_SCH}><pattern><rule context="item">'
        '<assert test="@ok" flag="fatal" id="MANY">missing ok</assert>'
        "</rule></pattern></schema>",
        f"<root>{items}</root>",
        max_findings=_FINDINGS_CAP,
    )

    assert summary.error_count == _OVERFLOW_FINDINGS
    assert len(summary.findings) == _FINDINGS_CAP
    assert summary.findings_truncated
    assert summary.findings_suppressed_count == _SUPPRESSED_FINDINGS


def test_truncation_keeps_errors_ahead_of_warnings_and_info():
    """Under a tight cap, ERROR findings survive before WARNING/INFO (D10).

    Nine findings (three each of ERROR/WARNING/INFO) capped at three keep the
    three ERRORs — the most actionable band — rather than an arbitrary
    document-order slice that might drop every error.
    """
    summary = _run(
        f'<schema {_SCH}><pattern><rule context="item">'
        '<assert test="@a" flag="info" id="I">i</assert>'
        '<assert test="@b" flag="warning" id="W">w</assert>'
        '<assert test="@c" flag="fatal" id="E">e</assert>'
        "</rule></pattern></schema>",
        "<root><item/><item/><item/></root>",
        max_findings=3,
    )

    assert [f.severity for f in summary.findings] == [SEVERITY_ERROR] * 3


def test_duplicate_rule_id_collapses_to_the_most_severe_in_the_map():
    """One id at two severities collapses to the most severe in the CEL map.

    ``finding_rule_ids_by_severity`` answers "did rule X fire, and how bad?"; if
    the same id appears as both WARNING and ERROR, the map must report ERROR so
    a severity gate cannot be fooled by the milder occurrence. The findings list
    still keeps BOTH rows. Built from crafted SVRL because lxml will not compile
    a schema with a duplicate id (see the compile-time test above).
    """
    crafted = (
        f'<svrl:schematron-output xmlns:svrl="{SVRL_NS}">'
        '<svrl:fired-rule context="a"/>'
        '<svrl:failed-assert id="DUP" flag="warning" location="/a">'
        "<svrl:text>w</svrl:text></svrl:failed-assert>"
        '<svrl:fired-rule context="b"/>'
        '<svrl:failed-assert id="DUP" flag="fatal" location="/b">'
        "<svrl:text>e</svrl:text></svrl:failed-assert>"
        "</svrl:schematron-output>"
    )
    summary = parse_svrl(crafted.encode("utf-8"))

    assert summary.finding_rule_ids_by_severity == {"DUP": SEVERITY_ERROR}
    assert len(summary.findings) == _DUPLICATE_ID_ROWS
    assert summary.error_count == 1
    assert summary.warning_count == 1


def test_unicode_round_trips_through_finding_messages():
    """Non-ASCII text in messages and interpolated values survives parsing.

    Schematron packs are authored in many languages; a finding message with
    German text and an interpolated product name containing a snowman must come
    back byte-for-byte, or localized packs would corrupt their own output.
    """
    summary = _run(
        f'<schema {_SCH}><pattern><rule context="produkt">'
        '<assert test="false()" flag="fatal" id="U">'
        'Ungueltiger Preis fuer <value-of select="@naam"/> unter Mindestwert.'
        "</assert></rule></pattern></schema>",
        "<produkt naam='Kuehlschrank ☃'/>",
    )

    assert (
        summary.findings[0].message
        == "Ungueltiger Preis fuer Kuehlschrank ☃ unter Mindestwert."
    )


@pytest.mark.parametrize("blob", [b"", b"   \n  "])
def test_empty_or_whitespace_svrl_raises_rather_than_silently_passing(blob):
    """Empty / whitespace SVRL is an error, never an accidental clean report.

    If the engine produced no usable report, the parser must refuse it — a
    blank report silently parsed as "zero findings" would be the worst kind of
    false green.
    """
    with pytest.raises(SvrlParseError):
        parse_svrl(blob)


# ── Neutral domain end-to-end: the purchase-order pack ───────────────────────
# A realistic, multi-rule, multi-severity pack in a non-invoice domain (see
# tests/assets/schematron/purchase_order/README.md). These prove the mechanics
# above compose in a document that looks like real work, not a one-rule probe.


def test_valid_order_passes_with_rules_actually_evaluated():
    """The clean order passes AND proves the rules ran (no false green).

    Zero findings of any severity, but all four rule contexts (order, two
    lines, audit) matched — ``fired_rule_count`` guards against a pass produced
    by a schema that matched nothing.
    """
    summary = _run_pack("purchase_order.sch", "purchase_order_valid.xml")

    assert summary.findings == []
    assert summary.error_count == 0
    assert summary.fired_rule_count == _PO_RULE_CONTEXTS
    assert summary.passed


def test_warnings_only_order_passes_despite_warning_and_info_findings():
    """An order with only WARNING/INFO findings still PASSES (D3).

    A run passes iff it has zero ERROR findings. This order trips the deprecated
    audit-status ``report`` and a missing description (both WARNING) plus a
    missing note (INFO) — three findings, yet ``passed`` is True. This is the
    flagship "warnings are advisory, not blocking" behaviour.
    """
    summary = _run_pack("purchase_order.sch", "purchase_order_warnings_only.xml")

    assert summary.error_count == 0
    assert summary.warning_count == _PO_WARNING_COUNT
    assert summary.info_count == 1
    assert summary.passed
    assert summary.finding_rule_ids_by_severity == {
        "VBPO-LEGACY-01": SEVERITY_WARNING,
        "VBPO-DESC-01": SEVERITY_WARNING,
        "VBPO-NOTE-01": SEVERITY_INFO,
    }


def test_bad_math_order_fails_on_the_cross_field_arithmetic_rule():
    """A cross-element numeric defect fails the run — the capability XSD lacks.

    The document is well-formed and every line is internally consistent, yet the
    order-level ``grandTotal = sum(lineTotals)`` rule fails: a constraint no
    grammar can express. An unrelated missing description rides along as a
    WARNING, proving ERROR and WARNING coexist and that the ERROR is decisive.
    """
    summary = _run_pack("purchase_order.sch", "purchase_order_bad_math.xml")

    assert summary.error_count == 1
    assert summary.warning_count == 1
    assert not summary.passed
    error = next(f for f in summary.findings if f.severity == SEVERITY_ERROR)
    assert error.rule_id == "VBPO-MATH-02"
    # value-of interpolated both the claimed and the computed totals.
    assert "999.00" in error.message
    assert "35" in error.message
