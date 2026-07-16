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
from validibot.submissions.models import SubmissionInputFile
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import ResolvedInputTrace
from validibot.validations.models import RunEvidenceArtifact
from validibot.validations.models import RunEvidenceArtifactAvailability
from validibot.validations.services.evidence import EvidenceManifestBuilder
from validibot.validations.services.evidence import stamp_evidence_manifest
from validibot.validations.tests.factories import ArtifactFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.tests.factories import ValidatorResourceFileFactory
from validibot.workflows.tests.factories import WorkflowFactory
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.tests.factories import WorkflowStepResourceFactory

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
    run_overrides.setdefault(
        "submission",
        SubmissionFactory(
            workflow=workflow,
            org=workflow.org,
            project=workflow.project,
            user=workflow.user,
        ),
    )

    defaults = {
        "workflow": workflow,
        "status": ValidationRunStatus.SUCCEEDED,
        "ended_at": datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
    }
    defaults.update(run_overrides)
    return ValidationRunFactory(**defaults)


def _artifact_port(validator, *, contract_key, role, envelope_channel, data_format):
    """Create a validator-owned artifact input port for evidence tests."""

    return StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key=contract_key,
        native_name=contract_key,
        direction=StepIODirection.INPUT,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        envelope_channel=envelope_channel,
        role=role,
        data_format=data_format,
    )


def _energyplus_run_with_ports():
    """Build an EnergyPlus run with primary-model and weather file ports."""

    validator = ValidatorFactory(
        validation_type=ValidationType.ENERGYPLUS,
        version=2,
    )
    workflow = WorkflowFactory(
        allowed_file_types=[SubmissionFileType.TEXT],
        input_retention=SubmissionRetention.STORE_30_DAYS,
    )
    step = WorkflowStepFactory(
        workflow=workflow,
        validator=validator,
        name="Simulate Model",
        step_key="simulate",
        order=10,
    )
    primary_port = _artifact_port(
        validator,
        contract_key="primary_model",
        role="primary-model",
        envelope_channel=EnvelopeChannel.INPUT_FILES,
        data_format="energyplus_idf",
    )
    weather_port = _artifact_port(
        validator,
        contract_key="weather_file",
        role="weather",
        envelope_channel=EnvelopeChannel.RESOURCE_FILES,
        data_format=ResourceFileType.ENERGYPLUS_WEATHER,
    )
    run = _completed_run(workflow=workflow)
    run.submission.original_filename = "model.idf"
    run.submission.file_type = SubmissionFileType.TEXT
    run.submission.size_bytes = 123
    run.submission.checksum_sha256 = "1" * 64
    run.submission.save(
        update_fields=[
            "original_filename",
            "file_type",
            "size_bytes",
            "checksum_sha256",
            "modified",
        ],
    )
    step_run = ValidationStepRunFactory(
        validation_run=run,
        workflow_step=step,
        step_order=step.order,
        status=StepStatus.PASSED,
        validator_backend_image_digest="repo/eplus@sha256:" + "e" * 64,
    )
    return run, step, step_run, primary_port, weather_port


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
        assert manifest.workflow_version == str(run.workflow.version)
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
            # The Workflow field is ``mcp_enabled`` after the 2026-06-27
            # rename; the evidence contract still records it under the
            # frozen ``agent_access_enabled`` key (mapped in evidence.py).
            mcp_enabled=True,
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
        # Evidence contract keeps the legacy field name, sourced from
        # ``workflow.mcp_enabled``.
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


class TestBuildManifestArtifactLineage:
    """Artifact lineage evidence records runtime file-port provenance."""

    def test_build_records_primary_model_and_weather_resource_bindings(self):
        """EnergyPlus file ports appear without leaking private runtime URIs."""
        run, step, step_run, primary_port, weather_port = _energyplus_run_with_ports()
        weather_file = ValidatorResourceFileFactory(
            validator=step.validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            filename="weather.epw",
        )
        weather_resource = WorkflowStepResourceFactory(
            step=step,
            role="WEATHER_FILE",
            validator_resource_file=weather_file,
        )
        private_model_uri = "file:///private/workspace/input/model.idf"
        private_weather_uri = "gs://validibot-private/resources/weather.epw"
        ResolvedInputTrace.objects.create(
            step_run=step_run,
            io_definition=primary_port,
            input_contract_key="primary_model",
            source_scope_used=BindingSourceScope.SUBMISSION_FILE,
            source_data_path_used="primary_file_uri",
            resolved=True,
            used_default=False,
            value_snapshot={
                "source": BindingSourceScope.SUBMISSION_FILE,
                "port_key": "primary_model",
                "role": "primary-model",
                "uri": private_model_uri,
            },
        )
        ResolvedInputTrace.objects.create(
            step_run=step_run,
            io_definition=weather_port,
            input_contract_key="weather_file",
            source_scope_used=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path_used=ResourceFileType.ENERGYPLUS_WEATHER,
            resolved=True,
            used_default=False,
            value_snapshot=[
                {
                    "source": BindingSourceScope.WORKFLOW_RESOURCE,
                    "id": str(weather_resource.validator_resource_file_id),
                    "type": ResourceFileType.ENERGYPLUS_WEATHER,
                    "port_key": "weather_file",
                    "uri": private_weather_uri,
                },
            ],
        )

        manifest = EvidenceManifestBuilder.build(run)

        bindings = {b.target_port_key: b for b in manifest.artifact_input_bindings}
        assert set(bindings) == {"primary_model", "weather_file"}
        assert (
            bindings["primary_model"].source_scope == BindingSourceScope.SUBMISSION_FILE
        )
        assert bindings["primary_model"].source_submission_id == str(run.submission_id)
        assert bindings["primary_model"].source_filename == "model.idf"
        assert bindings["primary_model"].source_sha256 == "1" * 64
        assert (
            bindings["weather_file"].source_scope
            == BindingSourceScope.WORKFLOW_RESOURCE
        )
        assert bindings["weather_file"].source_resource_id == str(weather_file.pk)
        assert bindings["weather_file"].source_filename == "weather.epw"
        assert bindings["weather_file"].source_data_format == (
            ResourceFileType.ENERGYPLUS_WEATHER
        )
        raw = EvidenceManifestBuilder.serialise(manifest).decode("ascii")
        assert private_model_uri not in raw
        assert private_weather_uri not in raw

    def test_build_records_submitted_artifact_port_file_metadata(self):
        """Submitted files beyond the primary payload keep their own file id/hash."""
        run, step, step_run, _primary_port, weather_port = _energyplus_run_with_ports()
        submitted = SubmissionInputFile.objects.create(
            submission=run.submission,
            workflow_step=step,
            port_key="weather_file",
            original_filename="submitted-weather.epw",
            content_type="text/plain",
            size_bytes=99,
            checksum_sha256="2" * 64,
        )
        private_weather_uri = "file:///private/workspace/input/submitted-weather.epw"
        ResolvedInputTrace.objects.create(
            step_run=step_run,
            io_definition=weather_port,
            input_contract_key="weather_file",
            source_scope_used=BindingSourceScope.SUBMISSION_FILE,
            source_data_path_used="",
            resolved=True,
            used_default=False,
            value_snapshot={
                "source": BindingSourceScope.SUBMISSION_FILE,
                "port_key": "weather_file",
                "role": "weather",
                "uri": private_weather_uri,
            },
        )

        manifest = EvidenceManifestBuilder.build(run)

        binding = manifest.artifact_input_bindings[0]
        assert binding.source_submission_file_id == str(submitted.pk)
        assert binding.source_submission_id == str(run.submission_id)
        assert binding.source_filename == "submitted-weather.epw"
        assert binding.source_size_bytes == 99  # noqa: PLR2004
        assert binding.source_sha256 == "2" * 64
        raw = EvidenceManifestBuilder.serialise(manifest).decode("ascii")
        assert private_weather_uri not in raw

    def test_build_records_upstream_artifact_edge(self):
        """An upstream artifact input becomes an explicit producer→consumer edge."""
        run, step, step_run, primary_port, _weather_port = _energyplus_run_with_ports()
        upstream_step = WorkflowStepFactory(
            workflow=run.workflow,
            validator=step.validator,
            name="Build Model",
            step_key="build_model",
            order=5,
        )
        upstream_step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=upstream_step,
            step_order=upstream_step.order,
            status=StepStatus.PASSED,
        )
        artifact = ArtifactFactory(
            validation_run=run,
            step_run=upstream_step_run,
            workflow_step=upstream_step,
            contract_key="generated_model",
            label="model.epjson",
            content_type="application/json",
            storage_uri="gs://validibot-private/runs/run-1/model.epjson",
            size_bytes=456,
            sha256="a" * 64,
            role="generated-model",
            data_format="energyplus_epjson",
        )
        ResolvedInputTrace.objects.create(
            step_run=step_run,
            io_definition=primary_port,
            input_contract_key="primary_model",
            source_scope_used=BindingSourceScope.UPSTREAM_ARTIFACT,
            source_data_path_used="build_model.generated_model",
            upstream_step_key="build_model",
            resolved=True,
            used_default=False,
            value_snapshot={
                "source": BindingSourceScope.UPSTREAM_ARTIFACT,
                "port_key": "primary_model",
                "source_data_path": "build_model.generated_model",
                "artifact": {
                    "artifact_id": str(artifact.pk),
                    "run_id": str(run.pk),
                    "producer_step_key": "build_model",
                    "contract_key": "generated_model",
                    "filename": "model.epjson",
                    "uri": artifact.storage_uri,
                    "sha256": "a" * 64,
                },
            },
        )

        manifest = EvidenceManifestBuilder.build(run)

        produced = manifest.produced_artifacts[0]
        assert produced.artifact_id == str(artifact.pk)
        assert produced.producer_step_key == "build_model"
        assert produced.contract_key == "generated_model"
        assert produced.sha256 == "a" * 64
        binding = manifest.artifact_input_bindings[0]
        assert binding.source_artifact_id == str(artifact.pk)
        assert binding.producer_step_key == "build_model"
        assert binding.producer_contract_key == "generated_model"
        edge = manifest.artifact_lineage_edges[0]
        assert edge.source_artifact_id == str(artifact.pk)
        assert edge.source_step_key == "build_model"
        assert edge.target_step_key == "simulate"
        assert edge.target_port_key == "primary_model"
        raw = EvidenceManifestBuilder.serialise(manifest).decode("ascii")
        assert artifact.storage_uri not in raw

    def test_lineage_survives_artifact_byte_purge(self):
        """Evidence uses retained hashes/provenance after storage pointers clear."""
        run, step, step_run, primary_port, _weather_port = _energyplus_run_with_ports()
        upstream_step = WorkflowStepFactory(
            workflow=run.workflow,
            validator=step.validator,
            name="Build Model",
            step_key="build_model",
            order=5,
        )
        upstream_step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=upstream_step,
            step_order=upstream_step.order,
            status=StepStatus.PASSED,
        )
        private_uri = "gs://validibot-private/runs/run-1/purged-model.epjson"
        artifact = ArtifactFactory(
            validation_run=run,
            step_run=upstream_step_run,
            workflow_step=upstream_step,
            contract_key="generated_model",
            label="purged-model.epjson",
            content_type="application/json",
            storage_uri=private_uri,
            size_bytes=456,
            sha256="c" * 64,
            role="generated-model",
        )
        ResolvedInputTrace.objects.create(
            step_run=step_run,
            io_definition=primary_port,
            input_contract_key="primary_model",
            source_scope_used=BindingSourceScope.UPSTREAM_ARTIFACT,
            source_data_path_used="build_model.generated_model",
            upstream_step_key="build_model",
            resolved=True,
            used_default=False,
            value_snapshot={
                "source": BindingSourceScope.UPSTREAM_ARTIFACT,
                "port_key": "primary_model",
                "artifact": {
                    "artifact_id": str(artifact.pk),
                    "run_id": str(run.pk),
                    "producer_step_key": "build_model",
                    "contract_key": "generated_model",
                    "filename": "purged-model.epjson",
                    "uri": private_uri,
                    "sha256": "c" * 64,
                },
            },
        )
        artifact.storage_uri = ""
        artifact.file = ""
        artifact.save(update_fields=["storage_uri", "file", "modified"])

        manifest = EvidenceManifestBuilder.build(run)

        assert manifest.produced_artifacts[0].sha256 == "c" * 64
        assert manifest.produced_artifacts[0].filename == "purged-model.epjson"
        assert manifest.artifact_lineage_edges[0].source_sha256 == "c" * 64
        raw = EvidenceManifestBuilder.serialise(manifest).decode("ascii")
        assert private_uri not in raw


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
