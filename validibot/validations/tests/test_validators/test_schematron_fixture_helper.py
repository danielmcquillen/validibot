"""lxml fixture-helper roundtrip tests (ADR-2026-07-01 test layer A, item 2).

Runs the illustrative subset pack (``tests/assets/schematron/``) through
``lxml.isoschematron`` — the XSLT-1.0 **fixture-generating helper**, never a
production runtime — and parses the produced SVRL with ``svrl.py``. This
proves the two halves agree end-to-end: the engine's real SVRL output maps to
the findings, severities, and output values the parser contract promises.

Deliberately does NOT instantiate ``SchematronValidator``: the validator is
an ``AdvancedValidator`` that dispatches to a container and must never run an
XSLT engine in-process (D4's "one execution path"). Keeping lxml out of the
validator tests is what enforces that separation in the test suite.

Expected fixture behaviour (documented in the assets README and verified when
the ADR was authored):

- ``peppol_invoice_valid.xml`` → validate() True, zero findings.
- ``peppol_invoice_invalid.xml`` → validate() False, exactly ONE finding:
  ``VB-CO-15`` (flag=fatal → ERROR) located at ``…/LegalMonetaryTotal`` —
  an arithmetic rule no XSD can express, which is the validator's raison
  d'être.
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
SEVERITY_ERROR = svrl.SEVERITY_ERROR
parse_svrl = svrl.parse_svrl

ASSETS = Path("tests/assets/schematron")


def _run_subset_pack(invoice_filename: str):
    """Compile the subset .sch via lxml and validate one invoice fixture.

    Returns ``(validate_ok, SvrlSummary)`` — the helper's boolean verdict
    plus our parser's view of the SVRL report it produced.
    """
    schematron = isoschematron.Schematron(
        etree.parse(str(ASSETS / "en16931_subset.sch")),
        store_report=True,
    )
    document = etree.parse(str(ASSETS / invoice_filename))
    ok = schematron.validate(document)
    svrl_bytes = etree.tostring(schematron.validation_report)
    return ok, parse_svrl(svrl_bytes)


def test_valid_invoice_passes_with_zero_findings():
    """The reconciling invoice passes: no findings, but rules DID evaluate.

    Asserting ``fired_rule_count > 0`` guards against the false-green failure
    mode where an XPath/namespace mistake makes no rule context match — which
    would also produce "zero findings" while checking nothing.
    """
    ok, summary = _run_subset_pack("peppol_invoice_valid.xml")

    assert ok is True
    assert summary.findings == []
    assert summary.error_count == 0
    assert summary.passed
    assert summary.fired_rule_count > 0


def test_invalid_invoice_fails_vb_co_15_at_legal_monetary_total():
    """The seeded totals defect yields exactly one ERROR: VB-CO-15.

    This is the flagship scenario: the invoice is well-formed and
    structurally valid, yet 100.00 + 21.00 != 120.00. The finding must carry
    the native rule id, the fatal flag, and a location pointing into
    LegalMonetaryTotal — everything a user needs to act on it.
    """
    ok, summary = _run_subset_pack("peppol_invoice_invalid.xml")

    assert ok is False
    assert summary.error_count == 1
    assert summary.warning_count == 0
    assert summary.info_count == 0
    assert len(summary.findings) == 1

    finding = summary.findings[0]
    assert finding.rule_id == "VB-CO-15"
    assert finding.severity == SEVERITY_ERROR
    assert finding.flag == "fatal"
    assert "LegalMonetaryTotal" in finding.location
    assert "TaxInclusiveAmount" in finding.message

    # The CEL-facing map carries the same verdict.
    assert summary.finding_rule_ids_by_severity == {"VB-CO-15": SEVERITY_ERROR}
    assert not summary.passed


def test_warning_only_invoice_passes_but_still_surfaces_the_warning():
    """An invoice with only a WARNING passes yet the warning is reported.

    A Schematron run passes iff there are zero ERROR findings (D3), so an
    otherwise-clean invoice whose supplier EndpointID lacks a schemeID trips the
    advisory VB-EAS-01 rule (flag="warning") without failing: ``passed`` is
    True, ``error_count`` is 0, and the warning is still visible to the author.
    This is the "warnings are advisory, not blocking" contract on the flagship
    invoice domain — the counterpart to the neutral purchase-order pack's
    warnings-only fixture.
    """
    ok, summary = _run_subset_pack("peppol_invoice_warning_only.xml")

    # lxml's validate() returns False whenever ANY assertion fired, even a
    # warning — but our severity-aware verdict is what the pipeline trusts.
    assert ok is False
    assert summary.error_count == 0
    assert summary.warning_count == 1
    assert summary.info_count == 0
    assert summary.passed

    warning = summary.findings[0]
    assert warning.rule_id == "VB-EAS-01"
    assert warning.severity == svrl.SEVERITY_WARNING
    assert summary.finding_rule_ids_by_severity == {"VB-EAS-01": svrl.SEVERITY_WARNING}
    # The run genuinely evaluated rules (guard against a false green).
    assert summary.fired_rule_count > 0
