"""Tests for the minimal evidence-manifest builder.

The builder is the Django-side boundary that turns a completed run into a
permanent receipt. These tests ensure it emits only the run outcome, workflow
step projection, and payload digests; persists the canonical bytes; preserves
digests independently of payload retention; and remains best-effort on
generation failure.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from unittest import mock

import pytest
from validibot_shared.evidence import SCHEMA_VERSION
from validibot_shared.evidence import EvidenceManifest

from validibot.submissions.constants import SubmissionRetention
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import RunEvidenceArtifactAvailability
from validibot.validations.services.evidence import EvidenceManifestBuilder
from validibot.validations.services.evidence import stamp_evidence_manifest
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db


def _completed_run(*, workflow=None, input_hash="a" * 64, output_hash="b" * 64):
    """Build a completed run with deterministic input/output identities."""

    if workflow is None:
        workflow = WorkflowFactory(
            input_retention=SubmissionRetention.STORE_30_DAYS,
        )
        WorkflowStepFactory(workflow=workflow)

    submission = SubmissionFactory(
        workflow=workflow,
        org=workflow.org,
        project=workflow.project,
        user=workflow.user,
        checksum_sha256=input_hash,
    )
    return ValidationRunFactory(
        workflow=workflow,
        submission=submission,
        status=ValidationRunStatus.SUCCEEDED,
        ended_at=datetime(2026, 8, 2, 4, 12, 55, tzinfo=UTC),
        output_hash=output_hash,
    )


class TestEvidenceManifestBuilder:
    """The builder emits the small public receipt rather than the run record."""

    def test_build_returns_minimal_manifest(self):
        """A completed run produces only the fields needed to explain it."""

        run = _completed_run()
        manifest = EvidenceManifestBuilder.build(run)

        assert isinstance(manifest, EvidenceManifest)
        assert manifest.schema_url == SCHEMA_VERSION
        assert manifest.run_id == str(run.id)
        assert manifest.completed_at == run.ended_at.isoformat()
        assert manifest.status == str(run.status)
        assert manifest.workflow.slug == run.workflow.slug
        assert manifest.workflow.version == str(run.workflow.version)
        assert manifest.input_sha256 == "a" * 64
        assert manifest.output_envelope_sha256 == "b" * 64
        assert set(manifest.model_dump(mode="json", by_alias=True)) == {
            "$schema",
            "run_id",
            "completed_at",
            "status",
            "workflow",
            "input_sha256",
            "output_envelope_sha256",
        }

    def test_build_projects_ordered_validator_steps_without_lineage(self):
        """Step identity explains execution order without a duplicate graph."""

        workflow = WorkflowFactory()
        first = WorkflowStepFactory(
            workflow=workflow,
            order=1,
            step_key="first",
        )
        second = WorkflowStepFactory(
            workflow=workflow,
            order=2,
            step_key="second",
        )
        run = _completed_run(workflow=workflow)

        manifest = EvidenceManifestBuilder.build(run)

        assert [step.key for step in manifest.workflow.steps] == [
            first.step_key,
            second.step_key,
        ]
        assert all(step.validator for step in manifest.workflow.steps)
        assert "lineage" not in manifest.model_dump(mode="json", by_alias=True)

    def test_output_digest_is_not_gated_by_output_retention(self):
        """Payload deletion policy does not delete the permanent output identity."""

        workflow = WorkflowFactory()
        WorkflowStepFactory(workflow=workflow)
        run = _completed_run(workflow=workflow, output_hash="c" * 64)

        manifest = EvidenceManifestBuilder.build(run)

        assert manifest.output_envelope_sha256 == "c" * 64

    def test_build_requires_completed_timestamp(self):
        """An incomplete run cannot receive an authoritative permanent receipt."""

        run = _completed_run()
        run.ended_at = None

        with pytest.raises(ValueError, match="before run completion"):
            EvidenceManifestBuilder.build(run)

    def test_serialise_uses_schema_alias_and_canonical_order(self):
        """The hashed bytes contain ``$schema`` and are deterministic."""

        manifest = EvidenceManifestBuilder.build(_completed_run())
        first = EvidenceManifestBuilder.serialise(manifest)
        second = EvidenceManifestBuilder.serialise(manifest)

        assert first == second
        assert b'"$schema":"https://validibot.com/schemas/' in first
        assert b"lineage" not in first


class TestStampEvidenceManifest:
    """Persistence and failure handling keep evidence advisory to run outcome."""

    def test_stamp_persists_canonical_manifest_and_hash(self):
        """The database hash matches the exact bytes stored in manifest_path."""

        run = _completed_run()

        artifact = stamp_evidence_manifest(run)

        assert artifact is not None
        assert artifact.availability == RunEvidenceArtifactAvailability.GENERATED
        artifact.manifest_path.open("rb")
        try:
            stored = artifact.manifest_path.read()
        finally:
            artifact.manifest_path.close()
        assert artifact.schema_version == SCHEMA_VERSION
        assert artifact.manifest_hash
        from validibot.core.filesafety import sha256_hexdigest

        assert sha256_hexdigest(stored) == artifact.manifest_hash

    def test_stamp_records_failure_without_raising(self):
        """Manifest errors do not change the completed run's outcome."""

        run = _completed_run()
        with mock.patch.object(
            EvidenceManifestBuilder,
            "build",
            side_effect=RuntimeError("receipt failed"),
        ):
            result = stamp_evidence_manifest(run)

        assert result is None
        artifact = run.evidence_artifact
        assert artifact.availability == RunEvidenceArtifactAvailability.FAILED
        assert artifact.generation_error == "receipt failed"
        assert run.status == ValidationRunStatus.SUCCEEDED
