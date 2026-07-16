"""Tests for the Schematron advanced validator (ADR-2026-07-01 test layer B).

``SchematronValidator`` is an :class:`AdvancedValidator`: Saxon/XSLT run only
in the isolated container backend (layer C, covered in
``validibot-validator-backends``). What this suite guards is the Django half,
fed with **canned output envelopes** — no engine ever runs here:

1. Schematron routes through the advanced (container) processor at all.
2. ``extract_output_values`` surfaces exactly the catalog ``o.*`` keys, and
   nulls the rule counts on an engine failure so a CEL gate can never read
   fake zeros (D9).
3. ``post_execute_validate`` rebuilds findings with the D10 contract —
   ``code`` = native rule id, ``meta`` carrying the location XPath and (when
   the step configures a documentation-URL template) a deep link — and maps
   engine failures to the single reserved ``schematron.*`` finding with
   ``meta.infra_error`` (D9): "we couldn't run the check" must never render
   as "your document failed the rules".
4. The output-value surface feeds CEL: an ``o.error_count == 0`` output
   assertion passes/fails with the envelope (ADR test-plan item 4).

Skips as a module when validibot-shared < 0.12.0 (the inline-rules
contract); activates automatically once the released package is synced.
"""

from __future__ import annotations

import pytest
from validibot_shared.schematron.envelopes import SchematronFinding
from validibot_shared.schematron.envelopes import SchematronOutputEnvelope
from validibot_shared.schematron.envelopes import SchematronOutputs
from validibot_shared.validations.envelopes import Severity as EnvelopeSeverity
from validibot_shared.validations.envelopes import ValidationMessage
from validibot_shared.validations.envelopes import ValidationStatus
from validibot_shared.validations.envelopes import ValidatorType

from validibot.validations.constants import ADVANCED_VALIDATION_TYPES
from validibot.validations.constants import AssertionType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.validators.schematron.validator import (
    CODE_BACKEND_UNAVAILABLE,
)
from validibot.validations.validators.schematron.validator import CODE_ENGINE_ERROR
from validibot.validations.validators.schematron.validator import CODE_ENGINE_TIMEOUT
from validibot.validations.validators.schematron.validator import (
    CODE_FINDINGS_TRUNCATED,
)
from validibot.validations.validators.schematron.validator import CODE_RULES_INVALID
from validibot.validations.validators.schematron.validator import SchematronValidator

if "schematron_sha256" not in SchematronOutputs.model_fields:
    pytest.skip(
        "requires validibot-shared >= 0.12.0 (inline Schematron rules contract)",
        allow_module_level=True,
    )

# Catalog output keys the Schematron ValidatorConfig declares.
# extract_output_values must return exactly these ("catalog is the contract").
CATALOG_OUTPUT_KEYS = {
    "passed",
    "error_count",
    "warning_count",
    "fired_rule_count",
    "finding_rule_ids_by_severity",
    "query_binding",
    "engine",
}

RULES_SHA = "b" * 64
SUPPRESSED_COUNT = 7
FIRED_RULES = 3
DOC_URL_TEMPLATE = "https://docs.example.test/rules/#{rule_id}"


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
        "schematron_sha256": RULES_SHA,
        "query_binding": "xslt2",
        "engine": "SaxonC-HE 12.9",
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


def _warning_only_outputs() -> SchematronOutputs:
    """Outputs for a run that PASSES but carries one WARNING and one INFO.

    Mirrors the ``purchase_order_warnings_only`` fixture: zero ERROR findings,
    so ``passed`` is True, but a warning (e.g. a deprecated-status ``report``)
    and an info finding are still present and must survive the mapping.
    """
    return _outputs(
        passed=True,
        error_count=0,
        warning_count=1,
        info_count=1,
        finding_rule_ids_by_severity={
            "VBPO-LEGACY-01": "WARNING",
            "VBPO-NOTE-01": "INFO",
        },
        findings=[
            SchematronFinding(
                rule_id="VBPO-LEGACY-01",
                message="This order uses a deprecated audit status.",
                severity="WARNING",
                location_xpath="/order/audit",
                flag="warning",
            ),
            SchematronFinding(
                rule_id="VBPO-NOTE-01",
                message="An order should carry a free-text note.",
                severity="INFO",
                location_xpath="/order",
                flag="info",
            ),
        ],
    )


# ── Routing ──────────────────────────────────────────────────────────────────


def test_schematron_is_an_advanced_validation_type():
    """SCHEMATRON must route to the container processor, never in-process.

    ``get_step_processor`` keys off ADVANCED_VALIDATION_TYPES; without
    membership, Schematron would run in the worker — uploaded rules are
    executable XSLT, the exact thing the D4 isolation exists to contain.
    """
    assert ValidationType.SCHEMATRON in ADVANCED_VALIDATION_TYPES


# ── extract_output_values ───────────────────────────────────────────────────


def test_extract_output_values_returns_catalog_keys_only():
    """Output values are exactly the catalog keys — no envelope-field leakage.

    ``info_count``/``execution_seconds``/``schematron_sha256`` are outputs
    but NOT catalog output_values; leaking them into ``o.*`` would break the
    "catalog is the contract" invariant every advanced validator holds.
    """
    envelope = _envelope(status=ValidationStatus.SUCCESS, outputs=_outputs())
    output_values = SchematronValidator().extract_output_values(envelope)

    assert set(output_values) == CATALOG_OUTPUT_KEYS
    assert output_values["passed"] is True
    assert output_values["error_count"] == 0
    assert output_values["engine"] == "SaxonC-HE 12.9"


def test_extract_output_values_none_when_no_outputs():
    """A crash-level envelope (outputs=None) yields no output_values, not a crash."""
    envelope = _envelope(status=ValidationStatus.FAILED_RUNTIME, outputs=None)
    assert SchematronValidator().extract_output_values(envelope) is None


def test_engine_failure_nulls_rule_outputs_instead_of_fake_zeros():
    """On engine failure the counts are None (unknown) and the map is empty.

    This is the D9 guard for CEL: with fake zeros, a gate like
    ``o.error_count == 0`` would read an engine crash as "no rule errors" —
    the exact overclaim the failure taxonomy forbids.
    """
    envelope = _envelope(
        status=ValidationStatus.FAILED_RUNTIME,
        outputs=_outputs(engine_status="error", engine_message="Saxon crashed"),
    )
    output_values = SchematronValidator().extract_output_values(envelope)

    assert output_values["passed"] is None
    assert output_values["error_count"] is None
    assert output_values["warning_count"] is None
    assert output_values["fired_rule_count"] is None
    assert output_values["finding_rule_ids_by_severity"] == {}


# ── post_execute_validate: the D10 findings contract ─────────────────────────


def test_findings_carry_native_rule_id_and_location():
    """Findings map with code=rule id and meta carrying the location XPath.

    The feature's value proposition is actionable, cross-referenceable rule
    ids: ``ValidationFinding.code`` holds the rule's id verbatim and the
    SVRL location points at the offending element.
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
    # No step context → no documentation template → no deep link.
    assert "rule_url" not in finding.meta


def test_clean_run_passes_and_records_provenance_stats():
    """A clean run passes and stats carry the D5 provenance set.

    The sha256 of the executed rules + the engine identity are what make a
    result reproducible. They land in step-run stats.
    """
    envelope = _envelope(status=ValidationStatus.SUCCESS, outputs=_outputs())
    result = SchematronValidator().post_execute_validate(envelope, run_context=None)

    assert result.passed is True
    assert result.issues == []
    assert result.stats["schematron_sha256"] == RULES_SHA
    assert result.stats["query_binding"] == "xslt2"
    assert result.stats["engine"] == "SaxonC-HE 12.9"
    assert result.stats["engine_status"] == "ok"


def test_truncated_findings_surface_an_explicit_marker():
    """A capped findings list is accompanied by one truncation finding (D10).

    Truncation is never silent: the synthetic ``schematron.findings_truncated``
    row states how many findings were suppressed, so "clean-ish" and
    "thousands of errors, capped" can never look the same.
    """
    outputs = _invalid_outputs().model_copy(
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
        ("error", "rules_invalid", CODE_RULES_INVALID),
        ("error", "backend_unavailable", CODE_BACKEND_UNAVAILABLE),
    ],
)
def test_engine_failures_produce_one_reserved_infra_finding(
    engine_status,
    engine_error_code,
    expected_code,
):
    """Engine failures yield ONE reserved finding flagged infra_error (D9).

    'We couldn't run the rules' ≠ 'your document failed the rules'. That
    includes ``rules_invalid`` — the author's uploaded rules failing to
    compile is an authoring problem, and the submitter's document must not
    be branded non-compliant because of it.
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
    assert result.output_values["finding_rule_ids_by_severity"] == {}


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


# ── post_execute_validate: severity mapping & non-error findings ─────────────


def test_warning_only_run_passes_and_still_surfaces_the_findings():
    """A run with only WARNING/INFO findings passes, findings still mapped (D3).

    ``passed`` follows the envelope status (SUCCESS here — zero ERRORs), yet the
    warning and info findings must survive ``post_execute_validate`` as issues
    with their native ids and mapped severities. "Passes" must never mean
    "silently drop the advisory findings".
    """
    envelope = _envelope(
        status=ValidationStatus.SUCCESS,
        outputs=_warning_only_outputs(),
    )
    result = SchematronValidator().post_execute_validate(envelope, run_context=None)

    assert result.passed is True
    by_code = {issue.code: issue for issue in result.issues}
    assert by_code["VBPO-LEGACY-01"].severity == Severity.WARNING
    assert by_code["VBPO-NOTE-01"].severity == Severity.INFO
    # No infrastructure/truncation rows — just the two real findings.
    assert set(by_code) == {"VBPO-LEGACY-01", "VBPO-NOTE-01"}


def test_finding_without_a_rule_id_maps_with_an_empty_code():
    """A finding whose SVRL element had no ``@id`` maps to an empty ``code``.

    Not every publisher assertion carries an id. The mapping must not crash or
    invent one, and with no id there can be no documentation deep link even when
    a template is configured — ``code`` is "" and ``rule_url`` is absent.
    """
    outputs = _outputs(
        passed=False,
        error_count=1,
        findings=[
            SchematronFinding(
                rule_id="",
                message="An unlabelled assertion failed.",
                severity="ERROR",
                location_xpath="/root/child",
                flag="fatal",
            ),
        ],
    )
    envelope = _envelope(status=ValidationStatus.FAILED_VALIDATION, outputs=outputs)
    result = SchematronValidator().post_execute_validate(envelope, run_context=None)

    finding = result.issues[0]
    assert finding.code == ""
    assert finding.path == "/root/child"
    assert "rule_url" not in finding.meta


def test_generic_engine_error_without_a_code_maps_to_engine_error():
    """An engine failure with no ``engine_error_code`` uses the catch-all code.

    The D9 taxonomy has a default: ``engine_status="error"`` with no machine
    hint is neither a timeout, a compile failure, nor a missing backend, so it
    surfaces as the reserved ``schematron.engine_error`` — still flagged
    ``infra_error`` so it never reads as a rule failure.
    """
    envelope = _envelope(
        status=ValidationStatus.FAILED_RUNTIME,
        outputs=_outputs(
            engine_status="error",
            engine_error_code="",
            engine_message="Saxon aborted unexpectedly.",
            passed=None,
        ),
    )
    result = SchematronValidator().post_execute_validate(envelope, run_context=None)

    assert len(result.issues) == 1
    finding = result.issues[0]
    assert finding.code == CODE_ENGINE_ERROR
    assert finding.meta["infra_error"] is True
    assert "Saxon aborted" in finding.message


def test_xslt1_query_binding_flows_through_to_provenance_stats():
    """The detected query binding is echoed verbatim into the D5 provenance.

    Whether a pack ran under xslt1 or xslt2 is part of what makes a result
    reproducible, so an xslt1 run must record ``query_binding == "xslt1"`` in
    stats — not silently normalise to the xslt2 default the other tests use.
    """
    envelope = _envelope(
        status=ValidationStatus.SUCCESS,
        outputs=_outputs(query_binding="xslt1", engine="lxml.isoschematron"),
    )
    result = SchematronValidator().post_execute_validate(envelope, run_context=None)

    assert result.stats["query_binding"] == "xslt1"
    assert result.output_values["query_binding"] == "xslt1"


# ── Deep-link URL safety + launch-time failure code preservation ─────────────


def test_rule_url_percent_encodes_the_rule_id():
    """The rule id is percent-encoded before it lands in the deep-link URL.

    The id comes from the author-controlled SVRL ``@id`` and is placed into a
    URL that clients render as a link, so characters that could break out of
    the URL/href (spaces, quotes, angle brackets) are encoded; a conventional
    rule id passes through unchanged.
    """
    make = SchematronValidator._rule_url

    encoded = make("https://docs.test/#{rule_id}", 'x y"<z>')
    assert " " not in encoded
    assert '"' not in encoded
    assert "<" not in encoded

    assert (
        make("https://docs.test/#{rule_id}", "VB-CO-15")
        == "https://docs.test/#VB-CO-15"
    )


def test_launch_time_infra_code_survives_to_the_result():
    """A launch-time reserved code + infra_error is preserved end-to-end (D9).

    Callback-time engine failures already carry ``schematron.*`` codes; this
    pins the LAUNCH path. A launcher result whose issue carries
    ``schematron.rules_invalid`` + ``meta.infra_error`` must flow through
    ``GCPExecutionBackend._launch_result_to_response`` and
    ``AdvancedValidator._response_to_result`` without collapsing to a generic
    coded-nothing error — so a launch-time failure renders as "we couldn't run
    the check", not as a rule failure.
    """
    from validibot.validations.services.execution.gcp import GCPExecutionBackend
    from validibot.validations.validators.base.base import ValidationIssue
    from validibot.validations.validators.base.base import ValidationResult

    launch_result = ValidationResult(
        passed=False,
        issues=[
            ValidationIssue(
                path="",
                message="The step's Schematron rules failed to compile.",
                severity=Severity.ERROR,
                code=CODE_RULES_INVALID,
                meta={"infra_error": True},
            ),
        ],
    )

    response = GCPExecutionBackend()._launch_result_to_response(launch_result)
    assert response.error_code == CODE_RULES_INVALID
    assert response.error_meta == {"infra_error": True}

    result = SchematronValidator()._response_to_result(response, is_async=True)
    assert result.passed is False
    issue = result.issues[0]
    assert issue.code == CODE_RULES_INVALID
    assert issue.meta.get("infra_error") is True


# ── Output values → CEL + the D10 deep link (DB-backed) ────────────────────────────


@pytest.mark.django_db
class TestOutputValuesFeedCelAssertions:
    """Prove the ``o.*`` surface drives real CEL output-stage assertions.

    A warnings-tolerant gate (``o.error_count == 0``) is the flagship D1
    authoring pattern; this test runs it through the actual assertion
    pipeline (DB rows, evaluator registry, CEL context) — not a mock — for
    both a passing and a failing envelope. The failing case also proves the
    D10 deep link: the step's ``rule_doc_url_template`` turns the finding's
    native id into a publisher-docs URL.
    """

    def _run_context_with_cel_gate(self):
        """Build a Schematron step whose ruleset carries rules + the gate."""
        from pathlib import Path

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
        rules_text = Path("tests/assets/schematron/en16931_subset.sch")
        ruleset = RulesetFactory(
            ruleset_type=RulesetType.SCHEMATRON,
            rules_text=rules_text.read_text(),
            metadata={"rule_doc_url_template": DOC_URL_TEMPLATE},
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
        return RunContext(validation_run=run, step=step, upstream_steps={})

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

    def test_gate_fails_and_finding_carries_the_deep_link(self):
        """error_count == 1 → the gate fails; the finding deep-links (D10)."""
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
        assert result.assertion_stats.failures == 1
        finding = next(i for i in result.issues if i.code == "VB-CO-15")
        assert finding.meta["rule_url"] == ("https://docs.example.test/rules/#VB-CO-15")
