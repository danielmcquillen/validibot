"""Tests for ``EvidenceManifestBuilder`` and ``stamp_evidence_manifest``.

ADR-2026-04-27 Phase 4 Session A: every completed validation run
gets a canonical-JSON manifest written to storage and a
``RunEvidenceArtifact`` row pinning its hash. These tests pin down
the Session A contract:

1. The builder produces a schema-valid manifest with the workflow's
   contract snapshot, validator metadata per step, and input schema.
2. The stamp function is best-effort — it records FAILED state
   instead of raising when something goes wrong, so a manifest
   problem never fails an otherwise-successful run.
3. The hash on the artifact row matches the SHA-256 of the
   canonical JSON bytes the manifest_path FileField persists.
4. Re-stamping is idempotent (same hash, same row updated in place).

What's *not* covered here
=========================

- Retention-aware redaction → Session B.
- Signed credential link / export UI → Session C.
- The cross-path wiring (sync orchestrator + async callback both
  call the stamper) is exercised indirectly by the existing
  end-to-end tests; here we test the stamper in isolation.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from unittest import mock

import pytest
from validibot_shared.evidence import SCHEMA_VERSION
from validibot_shared.evidence import EvidenceManifest

from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.constants import SubmissionRetention
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import RunEvidenceArtifact
from validibot.validations.models import RunEvidenceArtifactAvailability
from validibot.validations.services.evidence import EvidenceManifestBuilder
from validibot.validations.services.evidence import stamp_evidence_manifest
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


def _completed_run(workflow=None, **run_overrides):
    """Build a workflow + step + completed run for the builder to consume.

    Sets ``ended_at`` because Session A's manifest requires a
    terminal timestamp; the factory leaves it null by default.
    """
    if workflow is None:
        workflow = WorkflowFactory(
            allowed_file_types=[SubmissionFileType.JSON],
            input_retention=SubmissionRetention.STORE_30_DAYS,
        )
        WorkflowStepFactory(workflow=workflow)

    defaults = {
        "workflow": workflow,
        "status": ValidationRunStatus.SUCCEEDED,
        "ended_at": datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
    }
    defaults.update(run_overrides)
    return ValidationRunFactory(**defaults)


# ──────────────────────────────────────────────────────────────────────
# EvidenceManifestBuilder.build — schema content
# ──────────────────────────────────────────────────────────────────────


class TestBuildManifest:
    """The builder harvests a run into a schema-valid Pydantic model."""

    def test_build_returns_evidence_manifest_instance(self):
        """Happy path: the result validates against the v1 schema."""
        run = _completed_run()
        manifest = EvidenceManifestBuilder.build(run)
        assert isinstance(manifest, EvidenceManifest)
        assert manifest.schema_version == SCHEMA_VERSION

    def test_build_includes_run_identity(self):
        """run_id, workflow_slug, workflow_version, org_id, executed_at all present."""
        run = _completed_run()
        manifest = EvidenceManifestBuilder.build(run)
        assert manifest.run_id == str(run.id)
        assert manifest.workflow_id == run.workflow_id
        assert manifest.workflow_slug == run.workflow.slug
        assert manifest.workflow_version == run.workflow.version
        assert manifest.org_id == run.org_id
        # ``executed_at`` is the ISO 8601 form of run.ended_at — string
        # rather than datetime so canonical JSON is byte-stable.
        assert manifest.executed_at == run.ended_at.isoformat()

    def test_build_captures_workflow_contract_snapshot(self):
        """Every CONTRACT_FIELD value lives on manifest.workflow_contract."""
        workflow = WorkflowFactory(
            allowed_file_types=[
                SubmissionFileType.JSON,
                SubmissionFileType.XML,
            ],
            input_retention=SubmissionRetention.DO_NOT_STORE,
            agent_access_enabled=True,
        )
        WorkflowStepFactory(workflow=workflow)
        run = _completed_run(workflow=workflow)

        manifest = EvidenceManifestBuilder.build(run)
        contract = manifest.workflow_contract
        assert set(contract.allowed_file_types) == {
            SubmissionFileType.JSON,
            SubmissionFileType.XML,
        }
        assert contract.input_retention == SubmissionRetention.DO_NOT_STORE
        assert contract.agent_access_enabled is True

    def test_build_records_validator_per_step(self):
        """Each step with a validator contributes a StepValidatorRecord."""
        workflow = WorkflowFactory()
        step1 = WorkflowStepFactory(workflow=workflow, order=1)
        step2 = WorkflowStepFactory(workflow=workflow, order=2)
        # Force a known semantic_digest so the manifest mirrors it.
        step1.validator.semantic_digest = "a" * 64
        step1.validator.save(update_fields=["semantic_digest"])
        step2.validator.semantic_digest = "b" * 64
        step2.validator.save(update_fields=["semantic_digest"])
        run = _completed_run(workflow=workflow)

        manifest = EvidenceManifestBuilder.build(run)
        # Steps land in execution order.
        assert [s.step_order for s in manifest.steps] == [1, 2]
        digests = [s.validator_semantic_digest for s in manifest.steps]
        assert digests == ["a" * 64, "b" * 64]

    def test_build_records_empty_digest_as_none(self):
        """Custom validators with no digest -> manifest field is None.

        The manifest documents the absence rather than coercing it
        to an empty string. That makes "this step's validator has
        no proof" externally observable in the JSON.
        """
        workflow = WorkflowFactory()
        step = WorkflowStepFactory(workflow=workflow)
        step.validator.semantic_digest = ""
        step.validator.save(update_fields=["semantic_digest"])
        run = _completed_run(workflow=workflow)

        manifest = EvidenceManifestBuilder.build(run)
        assert manifest.steps[0].validator_semantic_digest is None

    def test_build_carries_input_schema_when_present(self):
        """Workflow.input_schema lands as manifest.input_schema."""
        schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
        workflow = WorkflowFactory(input_schema=schema)
        WorkflowStepFactory(workflow=workflow)
        run = _completed_run(workflow=workflow)

        manifest = EvidenceManifestBuilder.build(run)
        assert manifest.input_schema == schema

    def test_build_input_schema_none_when_workflow_has_no_contract(self):
        """No structured input contract -> manifest.input_schema is None."""
        workflow = WorkflowFactory(input_schema=None)
        WorkflowStepFactory(workflow=workflow)
        run = _completed_run(workflow=workflow)

        manifest = EvidenceManifestBuilder.build(run)
        assert manifest.input_schema is None

    # ── Manifest source field ──────────────────────────────────────
    #
    # ``ValidationRun.source`` is set by the launch path from the
    # authenticated route.  The manifest builder propagates it
    # verbatim so external verifiers can answer "what surface
    # produced this run?" without consulting the producer database
    # (which may have been purged under DO_NOT_STORE retention).
    #
    # The builder uses a forward-compat shim that emits the field
    # only when the installed ``validibot-shared`` schema accepts it.
    # These tests assume the installed shared library is recent
    # enough; if validibot-shared regresses the field, the tests
    # surface the regression at the integration boundary.
    def test_build_propagates_x402_source(self):
        """An x402-originated run records ``X402_AGENT`` in the manifest."""
        from validibot.validations.constants import ValidationRunSource

        workflow = WorkflowFactory()
        WorkflowStepFactory(workflow=workflow)
        run = _completed_run(
            workflow=workflow,
            source=ValidationRunSource.X402_AGENT,
        )

        manifest = EvidenceManifestBuilder.build(run)
        assert manifest.source == ValidationRunSource.X402_AGENT.value

    def test_build_propagates_api_source(self):
        """A REST-API-originated run records ``API`` in the manifest."""
        from validibot.validations.constants import ValidationRunSource

        workflow = WorkflowFactory()
        WorkflowStepFactory(workflow=workflow)
        run = _completed_run(
            workflow=workflow,
            source=ValidationRunSource.API,
        )

        manifest = EvidenceManifestBuilder.build(run)
        assert manifest.source == ValidationRunSource.API.value


# ──────────────────────────────────────────────────────────────────────
# EvidenceManifestBuilder.serialise — canonical JSON contract
# ──────────────────────────────────────────────────────────────────────


class TestSerialiseCanonicalJson:
    """The serialised bytes are stable; same manifest -> same bytes -> same hash."""

    def test_serialise_is_deterministic(self):
        """Calling serialise twice returns identical bytes."""
        run = _completed_run()
        manifest = EvidenceManifestBuilder.build(run)
        b1 = EvidenceManifestBuilder.serialise(manifest)
        b2 = EvidenceManifestBuilder.serialise(manifest)
        assert b1 == b2

    def test_serialise_is_ascii(self):
        """Bytes are always ASCII (ensure_ascii=True)."""
        run = _completed_run()
        manifest = EvidenceManifestBuilder.build(run)
        raw = EvidenceManifestBuilder.serialise(manifest)
        # All bytes < 128 means pure ASCII. Named constant rather
        # than the literal so PLR2004 sees the contract not magic.
        ascii_high_byte = 128
        assert all(b < ascii_high_byte for b in raw)

    def test_serialise_has_no_unnecessary_whitespace(self):
        """Compact JSON: no spaces between keys/values."""
        run = _completed_run()
        manifest = EvidenceManifestBuilder.build(run)
        raw = EvidenceManifestBuilder.serialise(manifest).decode("ascii")
        # Compact format produces no ", " or ": " separators.
        assert ", " not in raw
        assert ": " not in raw


# ──────────────────────────────────────────────────────────────────────
# EvidenceManifestBuilder.persist — DB row + hash
# ──────────────────────────────────────────────────────────────────────


class TestPersist:
    """Persisting the manifest writes to storage and pins the hash."""

    def test_persist_creates_artifact_row(self):
        """One row per run, populated with availability=GENERATED."""
        run = _completed_run()
        manifest = EvidenceManifestBuilder.build(run)
        artifact = EvidenceManifestBuilder.persist(run, manifest)

        assert isinstance(artifact, RunEvidenceArtifact)
        assert artifact.run_id == run.id
        assert artifact.availability == RunEvidenceArtifactAvailability.GENERATED
        assert artifact.schema_version == SCHEMA_VERSION

    def test_persist_writes_manifest_bytes_to_storage(self):
        """manifest_path FileField is set, file content is the canonical JSON."""
        run = _completed_run()
        manifest = EvidenceManifestBuilder.build(run)
        artifact = EvidenceManifestBuilder.persist(run, manifest)

        # Re-read the file from storage via the FileField.
        artifact.manifest_path.open("rb")
        try:
            written = artifact.manifest_path.read()
        finally:
            artifact.manifest_path.close()
        expected = EvidenceManifestBuilder.serialise(manifest)
        assert written == expected

    def test_manifest_hash_matches_serialised_bytes(self):
        """artifact.manifest_hash == sha256(serialised manifest)."""
        from validibot.core.filesafety import sha256_hexdigest

        run = _completed_run()
        manifest = EvidenceManifestBuilder.build(run)
        artifact = EvidenceManifestBuilder.persist(run, manifest)

        expected_hash = sha256_hexdigest(
            EvidenceManifestBuilder.serialise(manifest),
        )
        assert artifact.manifest_hash == expected_hash

    def test_persist_is_idempotent(self):
        """Calling persist twice updates the same row, same hash."""
        run = _completed_run()
        manifest = EvidenceManifestBuilder.build(run)

        a1 = EvidenceManifestBuilder.persist(run, manifest)
        a2 = EvidenceManifestBuilder.persist(run, manifest)

        # Same row, same hash.
        assert a1.pk == a2.pk
        assert a1.manifest_hash == a2.manifest_hash
        # Only one row exists.
        assert RunEvidenceArtifact.objects.filter(run=run).count() == 1


# ──────────────────────────────────────────────────────────────────────
# stamp_evidence_manifest — best-effort wrapper
# ──────────────────────────────────────────────────────────────────────


class TestStampEvidenceManifest:
    """The wrapper called from run-completion hooks must never raise."""

    def test_happy_path_returns_artifact(self):
        """Successful stamp returns a GENERATED artifact."""
        run = _completed_run()
        artifact = stamp_evidence_manifest(run)
        assert artifact is not None
        assert artifact.availability == RunEvidenceArtifactAvailability.GENERATED

    def test_builder_failure_records_failed_artifact(self):
        """Builder exception -> FAILED row + log, no re-raise.

        Patches the builder to raise. The wrapper must:
        1. NOT re-raise (the run continues).
        2. Record availability=FAILED with the exception message.
        """
        run = _completed_run()
        with mock.patch.object(
            EvidenceManifestBuilder,
            "build",
            side_effect=RuntimeError("boom"),
        ):
            artifact = stamp_evidence_manifest(run)

        # Returned None to caller (so the caller can carry on).
        assert artifact is None
        # FAILED row recorded with the exception message.
        recorded = RunEvidenceArtifact.objects.get(run=run)
        assert recorded.availability == RunEvidenceArtifactAvailability.FAILED
        assert "boom" in recorded.generation_error

    def test_failed_then_success_lifts_artifact_to_generated(self):
        """Re-stamp after fix: FAILED row gets updated to GENERATED."""
        run = _completed_run()

        # First attempt fails.
        with mock.patch.object(
            EvidenceManifestBuilder,
            "build",
            side_effect=RuntimeError("transient"),
        ):
            stamp_evidence_manifest(run)

        # Confirm it's recorded as FAILED.
        artifact = RunEvidenceArtifact.objects.get(run=run)
        assert artifact.availability == RunEvidenceArtifactAvailability.FAILED

        # Second attempt succeeds.
        result = stamp_evidence_manifest(run)
        assert result is not None
        assert result.availability == RunEvidenceArtifactAvailability.GENERATED
        # generation_error is cleared on successful re-stamp.
        result.refresh_from_db()
        assert result.generation_error == ""

    def test_run_status_is_unchanged_by_manifest_failure(self):
        """A FAILED stamp must not mutate the run row.

        The run's status / ended_at / etc. are the source of truth
        for "did the validation succeed?". Manifest generation is
        observability — it must never alter run state.
        """
        run = _completed_run(status=ValidationRunStatus.SUCCEEDED)
        original_status = run.status

        with mock.patch.object(
            EvidenceManifestBuilder,
            "build",
            side_effect=RuntimeError("nope"),
        ):
            stamp_evidence_manifest(run)

        run.refresh_from_db()
        assert run.status == original_status
