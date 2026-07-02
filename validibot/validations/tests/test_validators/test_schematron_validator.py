"""Tests for the Schematron advanced validator (ADR-2026-07-01 test layer B).

``SchematronValidator`` is an :class:`AdvancedValidator`: Saxon/XSLT run only
in the isolated container backend (layer C, covered in
``validibot-validator-backends``). What this suite guards is the Django half,
fed with **canned output envelopes** — no engine ever runs here:

1. Schematron routes through the advanced (container) processor at all.
2. ``extract_output_signals`` surfaces exactly the catalog ``o.*`` keys, and
   nulls the rule counts on an engine failure so a CEL gate can never read
   fake zeros (D9).
3. ``post_execute_validate`` rebuilds findings with the D10 contract —
   ``code`` = native rule id, ``meta`` carrying the location XPath and the
   publisher deep link — and maps engine failures to the single reserved
   ``schematron.*`` finding with ``meta.infra_error`` (D9): "we couldn't run
   the check" must never render as "your invoice failed the rules".
4. The signal surface actually feeds CEL: an ``o.error_count == 0`` output
   assertion passes/fails with the envelope (ADR test-plan item 4).

Skips as a module when validibot-shared < 0.11.0 (no
``validibot_shared.schematron``); the tests activate automatically once the
released package is synced into the venv.
"""

from __future__ import annotations

import pytest
from validibot_shared.validations.envelopes import Severity as EnvelopeSeverity
from validibot_shared.validations.envelopes import ValidationMessage
from validibot_shared.validations.envelopes import ValidationStatus
from validibot_shared.validations.envelopes import ValidatorType

from validibot.validations.constants import ADVANCED_VALIDATION_TYPES
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.validators.schematron.packs import SchematronPack
from validibot.validations.validators.schematron.packs import register_pack
from validibot.validations.validators.schematron.packs import unregister_pack
from validibot.validations.validators.schematron.validator import CODE_ARTIFACT_MISMATCH
from validibot.validations.validators.schematron.validator import (
    CODE_BACKEND_UNAVAILABLE,
)
from validibot.validations.validators.schematron.validator import CODE_ENGINE_TIMEOUT
from validibot.validations.validators.schematron.validator import (
    CODE_FINDINGS_TRUNCATED,
)
from validibot.validations.validators.schematron.validator import SchematronValidator

schematron_envelopes = pytest.importorskip(
    "validibot_shared.schematron.envelopes",
    reason="requires validibot-shared >= 0.11.0 (validibot_shared.schematron)",
)
SchematronFinding = schematron_envelopes.SchematronFinding
SchematronOutputEnvelope = schematron_envelopes.SchematronOutputEnvelope
SchematronOutputs = schematron_envelopes.SchematronOutputs

# Catalog signal keys the Schematron ValidatorConfig declares.
# extract_output_signals must return exactly these ("catalog is the contract").
CATALOG_SIGNAL_KEYS = {
    "passed",
    "error_count",
    "warning_count",
    "fired_rule_count",
    "finding_rule_ids_by_severity",
    "pack_id",
    "pack_version",
    "query_binding",
    "engine",
}

PACK_ID = "vb-peppol-subset"
PACK_VERSION = "0.1.0"
SUPPRESSED_COUNT = 7
FIRED_RULES = 3


@pytest.fixture
def vb_pack():
    """Register a temporary vetted pack (with a doc-URL template) and clean up.

    The D10 deep-link mapping resolves ``rule_url`` through the pack
    registry, so tests that assert on ``meta['rule_url']`` need a registered
    pack matching the envelope's pack_id/pack_version.
    """
    pack = SchematronPack(
        id=PACK_ID,
        title="VB Peppol subset",
        version=PACK_VERSION,
        syntax="ubl",
        source_url="https://example.test/packs/vb-peppol-subset",
        license="MIT",
        query_binding="xslt1",
        artifact="tests/assets/schematron/peppol_billing_subset.sch",
        source_sha256="a" * 64,
        artifact_sha256="b" * 64,
        engine="lxml-xslt1",
        rule_doc_url_template="https://docs.example.test/rules/#{rule_id}",
    )
    register_pack(pack)
    yield pack
    unregister_pack(pack.id, pack.version)


def _outputs(**overrides) -> SchematronOutputs:
    """Build SchematronOutputs with sensible pass-shaped defaults."""
    base = {
        "engine_status": "ok",
        "passed": True,
        "error_count": 0,
        "warning_count": 0,
        "info_count": 0,
        "fired_rule_count": FIRED_RULES,
        "finding_rule_ids_by_severity": {},
        "findings": [],
        "pack_id": PACK_ID,
        "pack_version": PACK_VERSION,
        "pack_source_sha256": "a" * 64,
        "pack_artifact_sha256": "b" * 64,
        "query_binding": "xslt1",
        "engine": "SaxonC-HE 12.5",
        "execution_seconds": 0.2,
    }
    base.update(overrides)
    return SchematronOutputs(**base)


def _envelope(
    *,
    status: ValidationStatus,
    outputs: SchematronOutputs | None,
    messages: list[ValidationMessage] | None = None,
) -> SchematronOutputEnvelope:
    return SchematronOutputEnvelope(
        run_id="run-1",
        validator={"id": "v1", "type": ValidatorType.SCHEMATRON, "version": "1"},
        status=status,
        timing={},
        messages=messages or [],
        outputs=outputs,
    )


def _invalid_outputs() -> SchematronOutputs:
    """Outputs matching the invalid-invoice fixture: one VB-CO-15 ERROR."""
    return _outputs(
        passed=False,
        error_count=1,
        finding_rule_ids_by_severity={"VB-CO-15": "ERROR"},
        findings=[
            SchematronFinding(
                rule_id="VB-CO-15",
                message=(
                    "Invoice total with VAT must equal total without VAT "
                    "plus the total VAT amount."
                ),
                severity="ERROR",
                location_xpath="/Invoice/LegalMonetaryTotal",
                flag="fatal",
            ),
        ],
    )


# ── Routing ──────────────────────────────────────────────────────────────────


def test_schematron_is_an_advanced_validation_type():
    """SCHEMATRON must route to the container processor, never in-process.

    ``get_step_processor`` keys off ADVANCED_VALIDATION_TYPES; without
    membership, Schematron would run in the worker — the Saxon/XSLT isolation
    D4 exists to prevent.
    """
    assert ValidationType.SCHEMATRON in ADVANCED_VALIDATION_TYPES


# ── extract_output_signals ───────────────────────────────────────────────────


def test_extract_output_signals_returns_catalog_keys_only():
    """Signals are exactly the catalog keys — no envelope-field leakage.

    ``info_count``/``execution_seconds``/checksums are outputs but NOT
    catalog signals; leaking them into ``o.*`` would break the "catalog is
    the contract" invariant every advanced validator holds.
    """
    envelope = _envelope(status=ValidationStatus.SUCCESS, outputs=_outputs())
    signals = SchematronValidator().extract_output_signals(envelope)

    assert set(signals) == CATALOG_SIGNAL_KEYS
    assert signals["passed"] is True
    assert signals["error_count"] == 0
    assert signals["pack_id"] == PACK_ID


def test_extract_output_signals_none_when_no_outputs():
    """A crash-level envelope (outputs=None) yields no signals, not a crash."""
    envelope = _envelope(status=ValidationStatus.FAILED_RUNTIME, outputs=None)
    assert SchematronValidator().extract_output_signals(envelope) is None


def test_engine_failure_nulls_rule_signals_instead_of_fake_zeros():
    """On engine failure the counts are None (unknown) and the map is empty.

    This is the D9 guard for CEL: with fake zeros, a gate like
    ``o.error_count == 0`` would read an engine crash as "no rule errors" —
    the exact overclaim the failure taxonomy forbids.
    """
    envelope = _envelope(
        status=ValidationStatus.FAILED_RUNTIME,
        outputs=_outputs(engine_status="error", engine_message="Saxon crashed"),
    )
    signals = SchematronValidator().extract_output_signals(envelope)

    assert signals["passed"] is None
    assert signals["error_count"] is None
    assert signals["warning_count"] is None
    assert signals["fired_rule_count"] is None
    assert signals["finding_rule_ids_by_severity"] == {}
    # Provenance still flows — you can see WHICH pack failed to run.
    assert signals["pack_id"] == PACK_ID


# ── post_execute_validate: the D10 findings contract ─────────────────────────


def test_findings_carry_native_rule_id_location_and_deep_link(vb_pack):
    """Findings map with code=rule id and meta carrying location + rule_url.

    The feature's value proposition is actionable, cross-referenceable rule
    ids: ``ValidationFinding.code`` holds the publisher's id verbatim and
    ``meta['rule_url']`` deep-links to the publisher's own rule text (D10).
    """
    envelope = _envelope(
        status=ValidationStatus.FAILED_VALIDATION,
        outputs=_invalid_outputs(),
    )
    result = SchematronValidator().post_execute_validate(envelope, run_context=None)

    assert result.passed is False
    finding = next(i for i in result.issues if i.code == "VB-CO-15")
    assert finding.severity == Severity.ERROR
    assert finding.path == "/Invoice/LegalMonetaryTotal"
    assert finding.meta["location_xpath"] == "/Invoice/LegalMonetaryTotal"
    assert finding.meta["flag"] == "fatal"
    assert finding.meta["pack_id"] == PACK_ID
    assert finding.meta["rule_url"] == "https://docs.example.test/rules/#VB-CO-15"


def test_clean_run_passes_and_records_provenance_stats():
    """A clean run passes and stats carry the full D5 provenance set.

    pack id/version alone don't let you reproduce a result — the checksums,
    query binding, and engine version do. They land in step-run stats.
    """
    envelope = _envelope(status=ValidationStatus.SUCCESS, outputs=_outputs())
    result = SchematronValidator().post_execute_validate(envelope, run_context=None)

    assert result.passed is True
    assert result.issues == []
    assert result.stats["pack_id"] == PACK_ID
    assert result.stats["pack_version"] == PACK_VERSION
    assert result.stats["pack_source_sha256"] == "a" * 64
    assert result.stats["pack_artifact_sha256"] == "b" * 64
    assert result.stats["query_binding"] == "xslt1"
    assert result.stats["engine"] == "SaxonC-HE 12.5"
    assert result.stats["engine_status"] == "ok"


def test_truncated_findings_surface_an_explicit_marker():
    """A capped findings list is accompanied by one truncation finding (D10).

    Truncation is never silent: the synthetic ``schematron.findings_truncated``
    row states how many findings were suppressed, so "clean-ish" and
    "thousands of errors, capped" can never look the same.
    """
    outputs = _invalid_outputs()
    outputs = outputs.model_copy(
        update={
            "findings_truncated": True,
            "findings_suppressed_count": SUPPRESSED_COUNT,
        },
    )
    envelope = _envelope(status=ValidationStatus.FAILED_VALIDATION, outputs=outputs)
    result = SchematronValidator().post_execute_validate(envelope, run_context=None)

    marker = next(i for i in result.issues if i.code == CODE_FINDINGS_TRUNCATED)
    assert marker.severity == Severity.WARNING
    assert marker.meta["suppressed_count"] == SUPPRESSED_COUNT
    assert str(SUPPRESSED_COUNT) in marker.message


# ── post_execute_validate: the D9 failure taxonomy ───────────────────────────


@pytest.mark.parametrize(
    ("engine_status", "engine_error_code", "expected_code"),
    [
        ("timeout", "", CODE_ENGINE_TIMEOUT),
        ("error", "artifact_mismatch", CODE_ARTIFACT_MISMATCH),
        ("error", "backend_unavailable", CODE_BACKEND_UNAVAILABLE),
    ],
)
def test_engine_failures_produce_one_reserved_infra_finding(
    engine_status,
    engine_error_code,
    expected_code,
):
    """Engine failures yield ONE reserved finding flagged infra_error (D9).

    'We couldn't run the rules' ≠ 'your invoice failed the rules'. The
    reserved non-rule code + ``meta.infra_error`` is what lets UI/API render
    these as infrastructure problems; no business-rule findings may be
    synthesised alongside them.
    """
    envelope = _envelope(
        status=ValidationStatus.FAILED_RUNTIME,
        outputs=_outputs(
            engine_status=engine_status,
            engine_error_code=engine_error_code,
            passed=None,
        ),
    )
    result = SchematronValidator().post_execute_validate(envelope, run_context=None)

    assert result.passed is False
    assert len(result.issues) == 1
    finding = result.issues[0]
    assert finding.code == expected_code
    assert finding.severity == Severity.ERROR
    assert finding.meta["infra_error"] is True
    # No rule ids anywhere: the run says nothing about rule compliance.
    assert result.signals["finding_rule_ids_by_severity"] == {}


def test_runtime_failure_without_outputs_preserves_envelope_messages():
    """A crash so early there are no outputs still surfaces the messages.

    The backend entrypoint uploads ``outputs=None`` for unexpected crashes;
    the generic envelope ``messages`` list is then the only user-facing
    explanation, so the override must fall back to it rather than returning
    an empty finding set.
    """
    envelope = _envelope(
        status=ValidationStatus.FAILED_RUNTIME,
        outputs=None,
        messages=[
            ValidationMessage(
                severity=EnvelopeSeverity.ERROR,
                text="Schematron backend failed before producing results.",
            ),
        ],
    )
    result = SchematronValidator().post_execute_validate(envelope, run_context=None)

    assert result.passed is False
    assert len(result.issues) == 1
    assert "failed before producing results" in result.issues[0].message


# ── Signals → CEL (ADR test-plan item 4) ─────────────────────────────────────


@pytest.mark.django_db
class TestSignalsFeedCelAssertions:
    """Prove the ``o.*`` surface drives real CEL output-stage assertions.

    A warnings-tolerant gate (``o.error_count == 0``) is the flagship D1
    authoring pattern; this test runs it through the actual assertion
    pipeline (DB rows, evaluator registry, CEL context) — not a mock — for
    both a passing and a failing envelope.
    """

    def _run_context_with_cel_gate(self):
        """Build a Schematron step whose ruleset carries the CEL gate."""
        from validibot.actions.protocols import RunContext
        from validibot.submissions.constants import SubmissionFileType
        from validibot.submissions.tests.factories import SubmissionFactory
        from validibot.validations.models import RulesetAssertion
        from validibot.validations.tests.factories import RulesetFactory
        from validibot.validations.tests.factories import ValidationRunFactory
        from validibot.validations.tests.factories import ValidatorFactory
        from validibot.workflows.tests.factories import WorkflowStepFactory

        validator = ValidatorFactory(
            validation_type=ValidationType.SCHEMATRON,
            is_system=False,
        )
        ruleset = RulesetFactory(
            ruleset_type=RulesetType.SCHEMATRON,
            rules_text="",
        )
        RulesetAssertion.objects.create(
            ruleset=ruleset,
            assertion_type=AssertionType.CEL_EXPRESSION,
            rhs={"expr": "o.error_count == 0"},
            cel_cache="o.error_count == 0",
            severity=Severity.ERROR,
            order=0,
            message_template="Schematron reported rule errors.",
        )
        submission = SubmissionFactory(
            content="<Invoice/>",
            file_type=SubmissionFileType.XML,
        )
        step = WorkflowStepFactory(validator=validator, ruleset=ruleset)
        run = ValidationRunFactory(workflow=step.workflow, submission=submission)
        return RunContext(validation_run=run, step=step, downstream_signals={})

    def test_gate_passes_for_clean_envelope(self):
        """error_count == 0 → the CEL gate passes and the step passes."""
        run_context = self._run_context_with_cel_gate()
        envelope = _envelope(status=ValidationStatus.SUCCESS, outputs=_outputs())

        result = SchematronValidator().post_execute_validate(
            envelope,
            run_context=run_context,
        )

        assert result.passed is True
        assert result.assertion_stats.total == 1
        assert result.assertion_stats.failures == 0

    def test_gate_fails_for_envelope_with_rule_errors(self):
        """error_count == 1 → the CEL gate fails alongside the rule finding."""
        run_context = self._run_context_with_cel_gate()
        envelope = _envelope(
            status=ValidationStatus.FAILED_VALIDATION,
            outputs=_invalid_outputs(),
        )

        result = SchematronValidator().post_execute_validate(
            envelope,
            run_context=run_context,
        )

        assert result.passed is False
        assert result.assertion_stats.total == 1
        assert result.assertion_stats.failures == 1
        assert any("rule errors" in i.message for i in result.issues)
