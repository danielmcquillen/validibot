"""Tests for the SHACL advanced validator (Django-side dispatch + result mapping).

SHACL is now an :class:`AdvancedValidator` — RDF parsing, pyshacl, and SPARQL
execution happen in the isolated container backend (covered by
``validibot-validator-backends``). What this suite guards is the Django half:

1. SHACL routes through the advanced (container) processor at all.
2. ``extract_output_signals`` surfaces exactly the catalog ``o.*`` keys.
3. ``post_execute_validate`` rebuilds findings from the container's structured
   ``outputs.findings`` (preserving SHACL ``meta`` and SPARQL-ASK
   ``assertion_id``), determines pass/fail from the envelope status, and surfaces
   the SHACL report in stats.
4. **The mixed-assertion partition** — the case raised in review: a step with
   both SHACL (SPARQL-ASK) and CEL/Basic assertions. The SHACL ones ran in the
   container; the Django pass must EXCLUDE them (no double-count, no re-run
   against a graph Django no longer has) and FOLD the container's tallies into
   the final totals.
"""

from __future__ import annotations

import pytest
from validibot_shared.shacl.envelopes import SHACLFinding
from validibot_shared.shacl.envelopes import SHACLOutputEnvelope
from validibot_shared.shacl.envelopes import SHACLOutputs
from validibot_shared.validations.envelopes import ValidationStatus
from validibot_shared.validations.envelopes import ValidatorType

from validibot.validations.constants import ADVANCED_VALIDATION_TYPES
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidationType
from validibot.validations.validators.shacl.validator import SHACLValidator

# Catalog signal keys the SHACL ValidatorConfig declares. extract_output_signals
# must return exactly these (the "catalog is the contract" rule).
CATALOG_SIGNAL_KEYS = {
    "parse_ok",
    "parse_serialization",
    "triple_count",
    "namespaces_present",
    "has_s223_namespace",
    "has_g36_namespace",
    "has_brick_namespace",
    "shacl_violation_count",
    "shacl_warning_count",
    "shacl_info_count",
    "shacl_total_count",
}

# Named test values (avoid magic literals in assertions).
SAMPLE_TRIPLE_COUNT = 42
SAMPLE_ASSERTION_ID = 42
EXPECTED_FOLDED_TOTAL = 2


def _outputs(**overrides) -> SHACLOutputs:
    """Build a SHACLOutputs with sensible defaults, overridable per test."""
    base = {
        "conforms": True,
        "parse_ok": True,
        "parse_serialization": "turtle",
        "triple_count": 10,
        "namespaces_present": ["http://example.org/"],
        "has_s223_namespace": False,
        "has_g36_namespace": False,
        "has_brick_namespace": False,
        "shacl_violation_count": 0,
        "shacl_warning_count": 0,
        "shacl_info_count": 0,
        "shacl_total_count": 0,
        "results_graph_turtle": "@prefix sh: <http://www.w3.org/ns/shacl#> .",
        "shacl_shapes_sha256": "abc",
        "advanced_shacl_requested": False,
        "shacl_result_handling": "fail_after_assertions",
        "assertion_total": 0,
        "assertion_failures": 0,
        "execution_seconds": 0.1,
    }
    base.update(overrides)
    return SHACLOutputs(**base)


def _envelope(
    *, status: ValidationStatus, outputs: SHACLOutputs
) -> SHACLOutputEnvelope:
    return SHACLOutputEnvelope(
        run_id="run-1",
        validator={"id": "v1", "type": ValidatorType.SHACL, "version": "2"},
        status=status,
        timing={},
        outputs=outputs,
    )


# ── Routing ──────────────────────────────────────────────────────────────────


def test_shacl_is_an_advanced_validation_type():
    """SHACL must be in ADVANCED_VALIDATION_TYPES so it routes to the container.

    ``get_step_processor`` keys off this set; without membership, SHACL would run
    in the in-process SimpleValidationProcessor — exactly the worker-side
    execution we moved away from for safety.
    """
    assert ValidationType.SHACL in ADVANCED_VALIDATION_TYPES


# ── extract_output_signals ───────────────────────────────────────────────────


def test_extract_output_signals_returns_catalog_keys_only():
    """Signals are exactly the catalog keys — no leakage of report/hash fields.

    Django's CEL/Basic output assertions evaluate against these signals; leaking
    non-catalog fields (the serialized report, hashes) into ``o.*`` would break
    the "catalog is the contract" invariant the other advanced validators hold.
    """
    envelope = _envelope(
        status=ValidationStatus.SUCCESS,
        outputs=_outputs(triple_count=SAMPLE_TRIPLE_COUNT, has_s223_namespace=True),
    )
    signals = SHACLValidator().extract_output_signals(envelope)

    assert set(signals) == CATALOG_SIGNAL_KEYS
    assert signals["triple_count"] == SAMPLE_TRIPLE_COUNT
    assert signals["has_s223_namespace"] is True
    assert "results_graph_turtle" not in signals


def test_extract_output_signals_none_when_no_outputs():
    """A runtime-failure envelope (outputs=None) yields no signals, not a crash."""
    envelope = SHACLOutputEnvelope(
        run_id="run-1",
        validator={"id": "v1", "type": ValidatorType.SHACL, "version": "2"},
        status=ValidationStatus.FAILED_RUNTIME,
        timing={},
        outputs=None,
    )
    assert SHACLValidator().extract_output_signals(envelope) is None


# ── post_execute_validate (no run_context: container-only path) ───────────────


def test_post_execute_rebuilds_findings_with_meta_and_assertion_id():
    """Findings come from outputs.findings with SHACL meta + assertion_id intact.

    The generic envelope ``messages`` list is lossy (no meta, no assertion_id).
    Rebuilding from the structured findings is what keeps SHACL focus-node /
    source-shape detail and SPARQL-ASK attribution available for display.
    """
    outputs = _outputs(
        conforms=False,
        shacl_violation_count=1,
        shacl_total_count=1,
        assertion_total=1,
        assertion_failures=1,
        findings=[
            SHACLFinding(
                path="ex:bob",
                message="Person needs a name.",
                severity="ERROR",
                code="shacl.MinCountConstraintComponent",
                meta={"shacl_focus_node": "ex:bob", "shacl_source_shape": "ex:Person"},
            ),
            SHACLFinding(
                message="ASK failed",
                severity="ERROR",
                code="shacl.sparql_ask_failed",
                assertion_id=SAMPLE_ASSERTION_ID,
            ),
        ],
    )
    envelope = _envelope(status=ValidationStatus.FAILED_VALIDATION, outputs=outputs)

    result = SHACLValidator().post_execute_validate(envelope, run_context=None)

    assert result.passed is False
    # SHACL violation finding keeps its meta and maps to ERROR.
    violation = next(
        i for i in result.issues if i.code.endswith("MinCountConstraintComponent")
    )
    assert violation.severity == Severity.ERROR
    assert violation.meta["shacl_focus_node"] == "ex:bob"
    assert violation.assertion_id is None
    # SPARQL-ASK finding keeps its assertion_id for attribution.
    ask = next(i for i in result.issues if i.code == "shacl.sparql_ask_failed")
    assert ask.assertion_id == SAMPLE_ASSERTION_ID
    # Container assertion tallies fold through (no run_context → no CEL added).
    assert result.assertion_stats.total == 1
    assert result.assertion_stats.failures == 1


def test_post_execute_success_passes_and_surfaces_report():
    """A conforming envelope with no findings passes; the report lands in stats."""
    envelope = _envelope(
        status=ValidationStatus.SUCCESS,
        outputs=_outputs(results_graph_turtle="REPORT"),
    )
    result = SHACLValidator().post_execute_validate(envelope, run_context=None)

    assert result.passed is True
    assert result.issues == []
    assert result.assertion_stats.total == 0
    # The serialized SHACL report is preserved for evidence download.
    assert result.stats["results_graph_turtle"] == "REPORT"
    assert result.stats["shacl_result_handling"] == "fail_after_assertions"


def test_post_execute_success_finding_maps_to_success_severity():
    """A SUCCESS-severity finding (passed SPARQL-ASK with a message) maps cleanly.

    The shared Severity enum has no SUCCESS member, so SHACLFinding carries it as
    a string; the validator must map it back to Django's Severity.SUCCESS rather
    than defaulting to ERROR.
    """
    outputs = _outputs(
        assertion_total=1,
        assertion_failures=0,
        findings=[
            SHACLFinding(
                message="Robot present.",
                severity="SUCCESS",
                code="assertion_passed",
                assertion_id=7,
            ),
        ],
    )
    envelope = _envelope(status=ValidationStatus.SUCCESS, outputs=outputs)
    result = SHACLValidator().post_execute_validate(envelope, run_context=None)

    success = next(i for i in result.issues if i.code == "assertion_passed")
    assert success.severity == Severity.SUCCESS


# ── The mixed-assertion partition (DB-backed integration) ────────────────────


@pytest.mark.django_db
class TestMixedAssertionPartition:
    """Prove the SHACL (container) + CEL/Basic (Django) split is lossless.

    This is the case raised in review: an author stacks SHACL SPARQL-ASK
    assertions and Basic/CEL assertions on one step. The SHACL ones execute in
    the container (and arrive pre-counted in ``outputs.assertion_total``); the
    Django pass must evaluate ONLY the non-SHACL ones and ADD its tally to the
    container's. Getting this wrong would either double-count the SHACL
    assertions or re-run them against a graph Django no longer holds.
    """

    def _run_context(self, ruleset):
        """Build a real run_context (validator + step.ruleset + run + submission).

        Real factories (not mocks) because post_execute_validate's CEL/Basic
        payload builder issues ORM queries keyed on the step/run primary keys.
        """
        from validibot.actions.protocols import RunContext
        from validibot.submissions.constants import SubmissionFileType
        from validibot.submissions.tests.factories import SubmissionFactory
        from validibot.validations.tests.factories import ValidationRunFactory
        from validibot.validations.tests.factories import ValidatorFactory
        from validibot.workflows.tests.factories import WorkflowStepFactory

        validator = ValidatorFactory(
            validation_type=ValidationType.SHACL,
            is_system=False,
        )
        submission = SubmissionFactory(
            content="@prefix ex: <http://example.org/> . ex:a a ex:Thing .",
            file_type=SubmissionFileType.TEXT,
        )
        step = WorkflowStepFactory(validator=validator, ruleset=ruleset)
        run = ValidationRunFactory(workflow=step.workflow, submission=submission)
        return validator, RunContext(
            validation_run=run, step=step, downstream_signals={}
        )

    def test_shacl_assertions_excluded_and_counts_fold(self):
        """One SHACL + one Basic assertion → container counts SHACL, Django the Basic.

        Expected: total = container(1 SHACL) + Django(1 Basic) = 2; the SHACL
        assertion is NOT re-evaluated in Django (it would appear as a duplicate or
        an engine-error finding if it were).
        """
        from validibot.validations.constants import RulesetType
        from validibot.validations.tests.factories import RulesetAssertionFactory
        from validibot.validations.tests.factories import RulesetFactory

        ruleset = RulesetFactory(
            ruleset_type=RulesetType.SHACL,
            rules_text="# shapes",
        )
        # A SHACL SPARQL-ASK assertion — runs in the container, NOT in Django.
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.SHACL,
            operator=AssertionOperator.SPARQL_ASK,
            target_data_path="shacl.data",
            severity=Severity.ERROR,
            rhs={"target_graph": "data", "query": "ASK { ?s ?p ?o }"},
        )
        # A Basic assertion against an output signal — runs in Django, output stage.
        RulesetAssertionFactory(
            ruleset=ruleset,
            assertion_type=AssertionType.BASIC,
            operator=AssertionOperator.EQ,
            target_data_path="shacl_violation_count",
            severity=Severity.ERROR,
            rhs={"value": 0},
        )

        validator, run_context = self._run_context(ruleset)

        # The container reports it evaluated the 1 SHACL ask (0 failures) and the
        # graph conformed (shacl_violation_count=0 so the Basic assertion passes).
        envelope = _envelope(
            status=ValidationStatus.SUCCESS,
            outputs=_outputs(
                shacl_violation_count=0,
                assertion_total=1,
                assertion_failures=0,
            ),
        )

        result = SHACLValidator().post_execute_validate(envelope, run_context)

        # Folded totals: 1 (container SHACL) + 1 (Django Basic) = 2.
        assert result.assertion_stats.total == EXPECTED_FOLDED_TOTAL
        assert result.assertion_stats.failures == 0
        assert result.passed is True
        # The SHACL assertion was excluded from the Django pass — no SPARQL-ASK
        # finding should be re-created here (the container owns that).
        assert not any(
            i.code in {"shacl.sparql_ask_failed", "shacl.sparql_ask_engine_error"}
            for i in result.issues
        )
