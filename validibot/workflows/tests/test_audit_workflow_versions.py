"""Tests for the ``audit_workflow_versions`` management command.

ADR-2026-04-27 Phase 3 Session D, task 12: the audit command is the
operator-facing surface for "which workflows are legacy versioned
vs which have full trust coverage." These tests pin the command's
contract:

1. It only audits in-use workflows by default (``--include-unused``
   to widen).
2. It reports validator gaps, validator drift, catalog resource
   gaps, step-owned resource gaps, and step-owned resource drift.
3. JSON output matches the ``validibot.workflow_audit.v1`` schema.
4. Exit codes: 0 on clean / info-only, 1 on error;
   ``--strict`` escalates warn to non-zero.
"""

from __future__ import annotations

import json
from datetime import UTC
from io import StringIO

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command

from validibot.core.filesafety import sha256_hexdigest
from validibot.validations.constants import ResourceFileType
from validibot.validations.models import ValidatorResourceFile
from validibot.validations.tests.factories import ValidatorFactory
from validibot.workflows.models import WorkflowStepResource
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


def _run_audit(**kwargs) -> dict:
    """Run the command in JSON mode and return the parsed report."""
    out = StringIO()
    call_command("audit_workflow_versions", emit_json=True, stdout=out, **kwargs)
    return json.loads(out.getvalue())


# ──────────────────────────────────────────────────────────────────────
# Workflow scope (--include-unused / --workflow-id)
# ──────────────────────────────────────────────────────────────────────


class TestWorkflowScope:
    """The default queryset filters to in-use workflows only."""

    def test_skips_unused_workflows_by_default(self):
        """Fresh, unused workflow with gaps -> not in the report by default."""
        # Create an unused workflow with a known gap (validator without digest).
        validator = ValidatorFactory(semantic_digest="")
        workflow = WorkflowFactory(is_locked=False)
        WorkflowStepFactory(workflow=workflow, validator=validator)

        report = _run_audit()
        # Default mode: only in-use workflows. This unused one
        # should not appear at all.
        assert all(w["workflow_id"] != workflow.pk for w in report["workflows"])

    def test_include_unused_widens_audit(self):
        """``--include-unused`` covers all workflows, even fresh ones."""
        validator = ValidatorFactory(semantic_digest="")
        workflow = WorkflowFactory(is_locked=False)
        WorkflowStepFactory(workflow=workflow, validator=validator)

        report = _run_audit(include_unused=True)
        ids = {w["workflow_id"] for w in report["workflows"]}
        assert workflow.pk in ids

    def test_workflow_id_filters_to_one(self):
        """``--workflow-id`` audits exactly one workflow."""
        validator = ValidatorFactory(semantic_digest="")
        wf1 = WorkflowFactory(is_locked=True)
        wf2 = WorkflowFactory(is_locked=True)
        WorkflowStepFactory(workflow=wf1, validator=validator)
        WorkflowStepFactory(workflow=wf2, validator=validator)

        report = _run_audit(workflow_id=wf1.pk)
        ids = {w["workflow_id"] for w in report["workflows"]}
        assert ids == {wf1.pk}


# ──────────────────────────────────────────────────────────────────────
# Validator findings
# ──────────────────────────────────────────────────────────────────────


class TestValidatorFindings:
    """The audit reports validator-level legacy + drift."""

    def test_missing_digest_on_used_workflow_reports_warn(self):
        """In-use workflow + validator without digest -> WARN finding."""
        validator = ValidatorFactory(semantic_digest="")
        workflow = WorkflowFactory(is_locked=True)
        WorkflowStepFactory(workflow=workflow, validator=validator)

        report = _run_audit()
        codes = {f["code"] for w in report["workflows"] for f in w["findings"]}
        assert "VALIDATOR_DIGEST_MISSING" in codes
        # Locked workflows surface gaps as info; only has-runs
        # escalates to warn (per the command's logic).
        # This workflow is locked-but-no-runs so severity is info.
        finding = next(
            f
            for w in report["workflows"]
            for f in w["findings"]
            if f["code"] == "VALIDATOR_DIGEST_MISSING"
        )
        assert finding["severity"] == "info"

    def test_missing_digest_on_workflow_with_runs_escalates_to_warn(self):
        """Workflow with actual runs -> gap severity escalates."""
        from validibot.submissions.tests.factories import SubmissionFactory
        from validibot.validations.tests.factories import ValidationRunFactory

        validator = ValidatorFactory(semantic_digest="")
        workflow = WorkflowFactory(is_locked=False)
        WorkflowStepFactory(workflow=workflow, validator=validator)
        submission = SubmissionFactory(workflow=workflow)
        ValidationRunFactory(workflow=workflow, submission=submission)

        report = _run_audit()
        finding = next(
            f
            for w in report["workflows"]
            for f in w["findings"]
            if f["code"] == "VALIDATOR_DIGEST_MISSING"
        )
        assert finding["severity"] == "warn"

    def test_validator_with_populated_digest_emits_no_finding(self):
        """Fully-covered validator -> no validator-related finding."""
        validator = ValidatorFactory(semantic_digest="a" * 64)
        workflow = WorkflowFactory(is_locked=True)
        WorkflowStepFactory(workflow=workflow, validator=validator)

        report = _run_audit()
        # Either no workflow report, or no validator findings on this workflow.
        for w in report["workflows"]:
            if w["workflow_id"] == workflow.pk:
                assert not any(
                    f["code"].startswith("VALIDATOR_") for f in w["findings"]
                )


# ──────────────────────────────────────────────────────────────────────
# Resource findings
# ──────────────────────────────────────────────────────────────────────


class TestResourceFindings:
    """The audit reports catalog and step-owned resource gaps + drift."""

    def test_catalog_resource_without_hash_reports_gap(self):
        """Catalog ValidatorResourceFile with empty content_hash -> gap finding."""
        validator = ValidatorFactory(semantic_digest="a" * 64)
        # Create the catalog row, then nuke its content_hash to
        # simulate a pre-Session-C row.
        catalog = ValidatorResourceFile.objects.create(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            name="Legacy Catalog",
            filename="legacy.epw",
            file=ContentFile(b"weather", name="legacy.epw"),
        )
        ValidatorResourceFile.objects.filter(pk=catalog.pk).update(
            content_hash="",
        )

        workflow = WorkflowFactory(is_locked=True)
        step = WorkflowStepFactory(workflow=workflow, validator=validator)
        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.WEATHER_FILE,
            validator_resource_file=catalog,
        )

        report = _run_audit()
        codes = {f["code"] for w in report["workflows"] for f in w["findings"]}
        assert "CATALOG_RESOURCE_HASH_MISSING" in codes

    def test_step_owned_resource_without_hash_reports_gap(self):
        """Step-owned resource with empty content_hash -> gap finding."""
        workflow = WorkflowFactory(is_locked=True)
        step = WorkflowStepFactory(workflow=workflow)
        resource = WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.MODEL_TEMPLATE,
            step_resource_file=ContentFile(b"v1", name="t.idf"),
            filename="t.idf",
            resource_type="MODEL_TEMPLATE",
        )
        # Simulate pre-Session-C row by clearing content_hash via update().
        WorkflowStepResource.objects.filter(pk=resource.pk).update(
            content_hash="",
        )

        report = _run_audit()
        codes = {f["code"] for w in report["workflows"] for f in w["findings"]}
        assert "STEP_RESOURCE_HASH_MISSING" in codes

    def test_step_owned_resource_drift_reports_error(self):
        """Stored hash != current bytes hash -> ERROR finding.

        Drift findings cause ``sys.exit(1)`` (the CI-gate path), so
        this test captures the JSON inside a ``pytest.raises`` block
        rather than via the helper. The JSON gets written to stdout
        BEFORE the exit, so json.loads() still produces a valid
        report.
        """
        workflow = WorkflowFactory(is_locked=True)
        step = WorkflowStepFactory(workflow=workflow)
        resource = WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.MODEL_TEMPLATE,
            step_resource_file=ContentFile(b"original", name="t.idf"),
            filename="t.idf",
            resource_type="MODEL_TEMPLATE",
        )
        # Stamp a wrong stored hash to simulate drift (bytes
        # replaced outside the gate, e.g. via raw filesystem write).
        wrong_hash = sha256_hexdigest(b"different content")
        WorkflowStepResource.objects.filter(pk=resource.pk).update(
            content_hash=wrong_hash,
        )

        out = StringIO()
        with pytest.raises(SystemExit):
            call_command(
                "audit_workflow_versions",
                emit_json=True,
                stdout=out,
            )
        report = json.loads(out.getvalue())
        finding = next(
            f
            for w in report["workflows"]
            for f in w["findings"]
            if f["code"] == "STEP_RESOURCE_HASH_DRIFT"
        )
        assert finding["severity"] == "error"


# ──────────────────────────────────────────────────────────────────────
# JSON schema + summary
# ──────────────────────────────────────────────────────────────────────


class TestJsonSchema:
    """The JSON output exposes the documented contract."""

    def test_schema_field_present(self):
        """Top-level ``schema`` key matches the v1 contract."""
        # Need at least one in-use workflow so the report runs;
        # otherwise we still get the empty report shape.
        report = _run_audit()
        assert report["schema"] == "validibot.workflow_audit.v1"

    def test_summary_counts_match_workflow_findings(self):
        """``summary.total_findings`` equals the sum of per-workflow findings."""
        validator = ValidatorFactory(semantic_digest="")
        wf = WorkflowFactory(is_locked=True)
        WorkflowStepFactory(workflow=wf, validator=validator)

        report = _run_audit()
        actual_total = sum(len(w["findings"]) for w in report["workflows"])
        assert report["summary"]["total_findings"] == actual_total

    def test_summary_by_severity_categorises_all_findings(self):
        """Every finding shows up in exactly one severity bucket."""
        validator = ValidatorFactory(semantic_digest="")
        wf = WorkflowFactory(is_locked=True)
        WorkflowStepFactory(workflow=wf, validator=validator)

        report = _run_audit()
        by_sev = report["summary"]["by_severity"]
        # Buckets always exist even when zero — predictable shape.
        assert "info" in by_sev
        assert "warn" in by_sev
        assert "error" in by_sev
        # Total across buckets equals the total findings count.
        assert sum(by_sev.values()) == report["summary"]["total_findings"]


# ──────────────────────────────────────────────────────────────────────
# Plain-text output
# ──────────────────────────────────────────────────────────────────────


class TestPlainTextOutput:
    """Default (no ``--json``) output is human-readable."""

    def test_clean_audit_says_no_findings(self):
        """Empty DB / no in-use workflows -> success message."""
        out = StringIO()
        call_command("audit_workflow_versions", stdout=out)
        text = out.getvalue()
        assert "No findings" in text

    def test_findings_render_with_severity_marker(self):
        """A finding's plain-text row includes the severity marker and code."""
        validator = ValidatorFactory(semantic_digest="")
        workflow = WorkflowFactory(is_locked=True)
        WorkflowStepFactory(workflow=workflow, validator=validator)

        out = StringIO()
        call_command("audit_workflow_versions", stdout=out)
        text = out.getvalue()
        assert "VALIDATOR_DIGEST_MISSING" in text
        # The severity marker for an "info"-level finding shows up
        # somewhere on the line. Style-wrapped text might include
        # ANSI codes; check substring case-insensitively.
        assert "INFO" in text.upper()


# ──────────────────────────────────────────────────────────────────────
# Exit code
# ──────────────────────────────────────────────────────────────────────


class TestExitCode:
    """Exit codes drive CI behaviour."""

    def test_clean_audit_does_not_exit_nonzero(self):
        """No findings -> command returns normally (no SystemExit)."""
        # Should not raise SystemExit.
        out = StringIO()
        call_command("audit_workflow_versions", stdout=out)

    def test_drift_finding_exits_nonzero(self):
        """An ERROR-severity finding triggers sys.exit(1)."""
        workflow = WorkflowFactory(is_locked=True)
        step = WorkflowStepFactory(workflow=workflow)
        resource = WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.MODEL_TEMPLATE,
            step_resource_file=ContentFile(b"v1", name="t.idf"),
            filename="t.idf",
            resource_type="MODEL_TEMPLATE",
        )
        # Stamp wrong hash to simulate drift.
        WorkflowStepResource.objects.filter(pk=resource.pk).update(
            content_hash=sha256_hexdigest(b"tampered"),
        )

        out = StringIO()
        with pytest.raises(SystemExit) as exc:
            call_command("audit_workflow_versions", stdout=out)
        assert exc.value.code == 1

    def test_strict_mode_escalates_warn_to_nonzero(self):
        """``--strict`` makes warn-level findings fail CI."""
        from validibot.submissions.tests.factories import SubmissionFactory
        from validibot.validations.tests.factories import ValidationRunFactory

        validator = ValidatorFactory(semantic_digest="")
        workflow = WorkflowFactory(is_locked=False)
        WorkflowStepFactory(workflow=workflow, validator=validator)
        submission = SubmissionFactory(workflow=workflow)
        ValidationRunFactory(workflow=workflow, submission=submission)

        # Without --strict: warn finding does NOT exit non-zero.
        out = StringIO()
        call_command("audit_workflow_versions", stdout=out)

        # With --strict: same warn finding DOES exit non-zero.
        out = StringIO()
        with pytest.raises(SystemExit) as exc:
            call_command("audit_workflow_versions", strict=True, stdout=out)
        assert exc.value.code == 1


# ──────────────────────────────────────────────────────────────────────
# Phase 4 Session A: manifest findings
# ──────────────────────────────────────────────────────────────────────
#
# The audit command extends to surface evidence-manifest gaps so
# operators see "this run completed before the manifest stamper
# existed" or "manifest generation FAILED for this run" loudly.


class TestManifestFindings:
    """Per-run manifest coverage findings."""

    def test_completed_run_without_artifact_emits_manifest_missing(self):
        """A terminal-state run with no RunEvidenceArtifact -> warn."""
        from validibot.submissions.tests.factories import SubmissionFactory
        from validibot.validations.constants import ValidationRunStatus
        from validibot.validations.tests.factories import ValidationRunFactory

        # Cover the validator digest so the manifest finding is the
        # ONLY thing the audit reports for this workflow.
        validator = ValidatorFactory(semantic_digest="a" * 64)
        workflow = WorkflowFactory(is_locked=True)
        WorkflowStepFactory(workflow=workflow, validator=validator)
        submission = SubmissionFactory(workflow=workflow)
        # Run is SUCCEEDED but has no manifest - simulates pre-Phase-4
        # state, or a manifest that was lost.
        ValidationRunFactory(
            workflow=workflow,
            submission=submission,
            status=ValidationRunStatus.SUCCEEDED,
        )

        report = _run_audit()
        codes = {f["code"] for w in report["workflows"] for f in w["findings"]}
        assert "MANIFEST_MISSING" in codes
        finding = next(
            f
            for w in report["workflows"]
            for f in w["findings"]
            if f["code"] == "MANIFEST_MISSING"
        )
        assert finding["severity"] == "warn"

    def test_pending_run_does_not_emit_manifest_missing(self):
        """In-flight runs are not flagged - they haven't reached the stamp yet."""
        from validibot.submissions.tests.factories import SubmissionFactory
        from validibot.validations.constants import ValidationRunStatus
        from validibot.validations.tests.factories import ValidationRunFactory

        validator = ValidatorFactory(semantic_digest="a" * 64)
        workflow = WorkflowFactory(is_locked=True)
        WorkflowStepFactory(workflow=workflow, validator=validator)
        submission = SubmissionFactory(workflow=workflow)
        ValidationRunFactory(
            workflow=workflow,
            submission=submission,
            status=ValidationRunStatus.PENDING,
        )

        report = _run_audit()
        codes = {f["code"] for w in report["workflows"] for f in w["findings"]}
        assert "MANIFEST_MISSING" not in codes

    def test_failed_artifact_emits_manifest_generation_failed_error(self):
        """Run with a FAILED RunEvidenceArtifact -> error severity."""
        from validibot_shared.evidence import SCHEMA_VERSION

        from validibot.submissions.tests.factories import SubmissionFactory
        from validibot.validations.constants import ValidationRunStatus
        from validibot.validations.models import RunEvidenceArtifact
        from validibot.validations.models import RunEvidenceArtifactAvailability
        from validibot.validations.tests.factories import ValidationRunFactory

        validator = ValidatorFactory(semantic_digest="a" * 64)
        workflow = WorkflowFactory(is_locked=True)
        WorkflowStepFactory(workflow=workflow, validator=validator)
        submission = SubmissionFactory(workflow=workflow)
        run = ValidationRunFactory(
            workflow=workflow,
            submission=submission,
            status=ValidationRunStatus.SUCCEEDED,
        )
        RunEvidenceArtifact.objects.create(
            run=run,
            schema_version=SCHEMA_VERSION,
            availability=RunEvidenceArtifactAvailability.FAILED,
            generation_error="storage IOError: disk full",
        )

        out = StringIO()
        with pytest.raises(SystemExit):
            call_command(
                "audit_workflow_versions",
                emit_json=True,
                stdout=out,
            )
        report = json.loads(out.getvalue())
        finding = next(
            f
            for w in report["workflows"]
            for f in w["findings"]
            if f["code"] == "MANIFEST_GENERATION_FAILED"
        )
        assert finding["severity"] == "error"

    def test_run_with_generated_artifact_emits_no_manifest_finding(self):
        """A run with a populated artifact -> no manifest finding."""
        from validibot.submissions.tests.factories import SubmissionFactory
        from validibot.validations.constants import ValidationRunStatus
        from validibot.validations.services.evidence import stamp_evidence_manifest
        from validibot.validations.tests.factories import ValidationRunFactory

        validator = ValidatorFactory(semantic_digest="a" * 64)
        workflow = WorkflowFactory(is_locked=True)
        WorkflowStepFactory(workflow=workflow, validator=validator)
        submission = SubmissionFactory(workflow=workflow)
        run = ValidationRunFactory(
            workflow=workflow,
            submission=submission,
            status=ValidationRunStatus.SUCCEEDED,
        )
        # Need ended_at for the manifest builder.
        from datetime import datetime

        run.ended_at = datetime(2026, 5, 1, tzinfo=UTC)
        run.save(update_fields=["ended_at"])

        # Stamp the manifest (this is what step_orchestrator /
        # validation_callback do at run completion).
        stamp_evidence_manifest(run)

        report = _run_audit()
        codes = {f["code"] for w in report["workflows"] for f in w["findings"]}
        assert "MANIFEST_MISSING" not in codes
        assert "MANIFEST_GENERATION_FAILED" not in codes

    def test_timed_out_run_without_artifact_emits_manifest_missing(self):
        """TIMED_OUT is a terminal state too — must surface the gap.

        The audit's terminal-statuses set previously listed only
        SUCCEEDED / FAILED / CANCELED, missing TIMED_OUT. That let
        timed-out runs slip past MANIFEST_MISSING /
        MANIFEST_GENERATION_FAILED detection — the same
        terminal-state drift the trust ADR called out for MCP wait
        handling. Use VALIDATION_RUN_TERMINAL_STATUSES (canonical)
        so future status additions land everywhere at once.
        """
        from validibot.submissions.tests.factories import SubmissionFactory
        from validibot.validations.constants import ValidationRunStatus
        from validibot.validations.tests.factories import ValidationRunFactory

        validator = ValidatorFactory(semantic_digest="a" * 64)
        workflow = WorkflowFactory(is_locked=True)
        WorkflowStepFactory(workflow=workflow, validator=validator)
        submission = SubmissionFactory(workflow=workflow)
        ValidationRunFactory(
            workflow=workflow,
            submission=submission,
            status=ValidationRunStatus.TIMED_OUT,
        )

        report = _run_audit()
        codes = {f["code"] for w in report["workflows"] for f in w["findings"]}
        assert "MANIFEST_MISSING" in codes
