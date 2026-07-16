"""Tests for step artifact reference registration and resolution.

Step artifacts are the workflow engine's data-plane counterpart to small
``output_values``. These tests lock in the v1 behavior: trusted backend
artifact metadata is indexed on the run, exposed through the canonical
``steps.<step_key>.artifact`` namespace, and resolvable by explicit bindings
without mutating payload or ordinary output values.
"""

from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from validibot_shared.validations.envelopes import ValidationArtifact

from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import StepIODirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepStatus
from validibot.validations.models import Artifact
from validibot.validations.models import WorkflowStepIOPromotion
from validibot.validations.services.artifacts import build_step_artifact_refs
from validibot.validations.services.artifacts import register_output_artifacts
from validibot.validations.services.path_resolution import resolve_step_input
from validibot.validations.services.run_context import RunContextBuilder
from validibot.validations.tests.factories import StepInputBindingFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db

UPDATED_REPORT_SIZE_BYTES = 200


class TestArtifactPromotionContract:
    """Promotion guards keep artifacts out of the workflow signal namespace."""

    def test_artifact_step_io_definition_rejects_promoted_signal_name(self):
        """An artifact port cannot masquerade as a small CEL/JSON signal value."""
        io_definition = StepIODefinitionFactory(
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
        )
        io_definition.promoted_signal_name = "report"

        with pytest.raises(
            ValidationError,
            match="Artifacts cannot be promoted to workflow signals",
        ):
            io_definition.full_clean()

    def test_artifact_step_io_definition_rejects_promotion_overlay(self):
        """Validator-owned artifact ports must also reject overlay promotion."""
        step = WorkflowStepFactory()
        io_definition = StepIODefinitionFactory(
            validator=step.validator,
            direction=StepIODirection.OUTPUT,
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
        )
        promotion = WorkflowStepIOPromotion(
            workflow_step=step,
            io_definition=io_definition,
            promoted_signal_name="report",
        )

        with pytest.raises(
            ValidationError,
            match="Only value ports can be promoted to workflow signals",
        ):
            promotion.full_clean()


class TestStepArtifactRegistration:
    """Artifact registration tests protect the run artifact index contract."""

    def test_registers_output_envelope_artifacts_as_artifact_refs(self):
        """A trusted backend artifact becomes one indexed ``ArtifactRef``."""

        run = ValidationRunFactory()
        step = WorkflowStepFactory(workflow=run.workflow, name="Simulate Model")
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
            step_order=step.order,
            status=StepStatus.PASSED,
            validator_backend_image_digest="repo/image@sha256:" + "a" * 64,
        )
        output_envelope = SimpleNamespace(
            artifacts=[
                ValidationArtifact(
                    name="eplusout.sql",
                    type="simulation-db",
                    mime_type="application/vnd.sqlite3",
                    uri="gs://validibot/runs/run-1/outputs/eplusout.sql",
                    size_bytes=123,
                ),
            ],
            raw_outputs=SimpleNamespace(
                manifest_uri="gs://validibot/runs/run-1/outputs/manifest.json",
            ),
        )

        refs = register_output_artifacts(
            step_run=step_run,
            output_envelope=output_envelope,
        )

        artifact = Artifact.objects.get(step_run=step_run)
        assert artifact.contract_key == "simulation_db"
        assert artifact.storage_uri == "gs://validibot/runs/run-1/outputs/eplusout.sql"
        assert artifact.manifest_uri.endswith("manifest.json")
        assert artifact.producer_validator_type == step.validator.validation_type
        assert refs == [build_step_artifact_refs(step_run)["simulation_db"]]
        assert refs[0]["schema_version"] == "validibot.artifact_ref.v1"
        assert refs[0]["producer_step_key"] == step.step_key
        assert refs[0]["uri"] == artifact.storage_uri

    def test_reregistering_same_artifact_updates_without_duplicates(self):
        """Callback retries must not duplicate the same step artifact output."""

        step_run = ValidationStepRunFactory(status=StepStatus.PASSED)
        first = SimpleNamespace(
            artifacts=[
                ValidationArtifact(
                    name="report.html",
                    type="report",
                    mime_type="text/html",
                    uri="gs://bucket/old/report.html",
                    size_bytes=100,
                ),
            ],
            raw_outputs=None,
        )
        second = SimpleNamespace(
            artifacts=[
                ValidationArtifact(
                    name="report.html",
                    type="report",
                    mime_type="text/html",
                    uri="gs://bucket/new/report.html",
                    size_bytes=200,
                ),
            ],
            raw_outputs=None,
        )

        register_output_artifacts(step_run=step_run, output_envelope=first)
        register_output_artifacts(step_run=step_run, output_envelope=second)

        artifacts = Artifact.objects.filter(step_run=step_run)
        assert artifacts.count() == 1
        artifact = artifacts.get()
        assert artifact.contract_key == "report"
        assert artifact.storage_uri == "gs://bucket/new/report.html"
        assert artifact.size_bytes == UPDATED_REPORT_SIZE_BYTES

    def test_declared_output_port_controls_artifact_ref_contract_key(self):
        """Output artifact ports should own stable public artifact keys.

        Backend envelopes use runner-facing roles such as ``simulation-db``.
        The declared output port maps that role to the author-facing
        ``eplusout_sql`` key used by ``steps.<step>.artifact.eplusout_sql``.
        """

        run = ValidationRunFactory()
        step = WorkflowStepFactory(workflow=run.workflow, name="Simulate Model")
        StepIODefinitionFactory(
            validator=step.validator,
            direction=StepIODirection.OUTPUT,
            contract_key="eplusout_sql",
            native_name="eplusout_sql",
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.DATASET,
            media_type="application/x-sqlite3",
            data_format="sqlite",
            accepted_data_formats=["sqlite"],
            accepted_media_types=["application/x-sqlite3", "application/vnd.sqlite3"],
            envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
            role="simulation-db",
            metadata={"accepted_extensions": ["sql"]},
            min_items=0,
            max_items=1,
        )
        step_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
            step_order=step.order,
            status=StepStatus.PASSED,
        )

        refs = register_output_artifacts(
            step_run=step_run,
            output_envelope=SimpleNamespace(
                artifacts=[
                    ValidationArtifact(
                        name="eplusout.sql",
                        type="simulation-db",
                        mime_type="application/x-sqlite3",
                        uri="gs://validibot/runs/run-1/outputs/eplusout.sql",
                        size_bytes=123,
                    ),
                ],
                raw_outputs=None,
            ),
        )

        artifact = Artifact.objects.get(step_run=step_run)
        assert artifact.contract_key == "eplusout_sql"
        assert artifact.role == "simulation-db"
        assert artifact.kind == ArtifactKind.DATASET
        assert artifact.data_format == "sqlite"
        assert artifact.metadata["source"] == "declared_output_port"
        assert refs == [build_step_artifact_refs(step_run)["eplusout_sql"]]

    def test_declared_output_port_rejects_wrong_artifact_extension(self):
        """Declared output ports should fail before indexing incompatible files."""

        step_run = ValidationStepRunFactory(status=StepStatus.PASSED)
        StepIODefinitionFactory(
            validator=step_run.workflow_step.validator,
            direction=StepIODirection.OUTPUT,
            contract_key="eplusout_sql",
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.DATASET,
            media_type="application/x-sqlite3",
            data_format="sqlite",
            accepted_data_formats=["sqlite"],
            accepted_media_types=["application/x-sqlite3"],
            envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
            role="simulation-db",
            metadata={"accepted_extensions": ["sql"]},
            min_items=0,
            max_items=1,
        )

        with pytest.raises(ValueError, match=r"expected one of \.sql"):
            register_output_artifacts(
                step_run=step_run,
                output_envelope=SimpleNamespace(
                    artifacts=[
                        ValidationArtifact(
                            name="eplusout.csv",
                            type="simulation-db",
                            mime_type="text/csv",
                            uri="gs://validibot/runs/run-1/outputs/eplusout.csv",
                            size_bytes=123,
                        ),
                    ],
                    raw_outputs=None,
                ),
            )

        assert Artifact.objects.filter(step_run=step_run).count() == 0

    def test_declared_output_port_enforces_scalar_cardinality(self):
        """A scalar output port must not silently index duplicate artifacts."""

        step_run = ValidationStepRunFactory(status=StepStatus.PASSED)
        StepIODefinitionFactory(
            validator=step_run.workflow_step.validator,
            direction=StepIODirection.OUTPUT,
            contract_key="eplusout_sql",
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
            artifact_kind=ArtifactKind.DATASET,
            media_type="application/x-sqlite3",
            data_format="sqlite",
            accepted_data_formats=["sqlite"],
            accepted_media_types=["application/x-sqlite3"],
            envelope_channel=EnvelopeChannel.OUTPUT_ARTIFACTS,
            role="simulation-db",
            metadata={"accepted_extensions": ["sql"]},
            min_items=0,
            max_items=1,
        )

        with pytest.raises(ValueError, match="accepts at most 1"):
            register_output_artifacts(
                step_run=step_run,
                output_envelope=SimpleNamespace(
                    artifacts=[
                        ValidationArtifact(
                            name="eplusout.sql",
                            type="simulation-db",
                            mime_type="application/x-sqlite3",
                            uri="gs://bucket/outputs/eplusout.sql",
                            size_bytes=100,
                        ),
                        ValidationArtifact(
                            name="copy.sql",
                            type="simulation-db",
                            mime_type="application/x-sqlite3",
                            uri="gs://bucket/outputs/copy.sql",
                            size_bytes=100,
                        ),
                    ],
                    raw_outputs=None,
                ),
            )

        assert Artifact.objects.filter(step_run=step_run).count() == 0


class TestStepArtifactRunContext:
    """Run context tests prove artifacts have a separate namespace."""

    def test_upstream_artifacts_are_exposed_separately_from_values(self):
        """Completed upstream step artifacts appear under ``artifact`` only."""

        run = ValidationRunFactory()
        upstream_step = WorkflowStepFactory(
            workflow=run.workflow,
            name="Build Model",
            order=10,
        )
        downstream_step = WorkflowStepFactory(
            workflow=run.workflow,
            name="Run Simulation",
            order=20,
        )
        upstream_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=upstream_step,
            step_order=10,
            status=StepStatus.PASSED,
            output_values={"site_eui": 42},
        )
        register_output_artifacts(
            step_run=upstream_run,
            output_envelope=SimpleNamespace(
                artifacts=[
                    ValidationArtifact(
                        name="model.epjson",
                        type="generated-model",
                        mime_type="application/json",
                        uri="gs://bucket/model.epjson",
                        size_bytes=456,
                    ),
                ],
                raw_outputs=None,
            ),
        )

        context = RunContextBuilder(run, downstream_step).build()

        upstream = context.upstream_steps[upstream_step.step_key]
        assert upstream["output"] == {"site_eui": 42}
        assert "generated_model" not in upstream["output"]
        assert upstream["artifact"]["generated_model"]["filename"] == "model.epjson"

    def test_upstream_artifact_binding_resolves_artifact_ref(self):
        """Bindings can target a prior step's artifact namespace explicitly."""

        run = ValidationRunFactory()
        upstream_step = WorkflowStepFactory(
            workflow=run.workflow,
            name="Build Model",
            order=10,
        )
        downstream_step = WorkflowStepFactory(
            workflow=run.workflow,
            name="Run Simulation",
            order=20,
        )
        upstream_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=upstream_step,
            step_order=10,
            status=StepStatus.PASSED,
        )
        register_output_artifacts(
            step_run=upstream_run,
            output_envelope=SimpleNamespace(
                artifacts=[
                    ValidationArtifact(
                        name="model.epjson",
                        type="generated-model",
                        mime_type="application/json",
                        uri="gs://bucket/model.epjson",
                        size_bytes=456,
                    ),
                ],
                raw_outputs=None,
            ),
        )
        io_definition = StepIODefinitionFactory(
            validator=None,
            workflow_step=downstream_step,
            direction=StepIODirection.INPUT,
            contract_key="primary_model",
            native_name="primary-model",
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
        )
        binding = StepInputBindingFactory(
            workflow_step=downstream_step,
            io_definition=io_definition,
            source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
            source_data_path=f"{upstream_step.step_key}.generated_model",
        )
        context = RunContextBuilder(run, downstream_step).build()

        resolved = resolve_step_input(
            binding,
            upstream_steps=context.upstream_steps,
        )

        assert resolved.resolved is True
        assert resolved.value["schema_version"] == "validibot.artifact_ref.v1"
        assert resolved.value["uri"] == "gs://bucket/model.epjson"
        assert resolved.upstream_step_key == upstream_step.step_key
