"""Tests for step artifact reference registration and resolution.

Step artifacts are the workflow engine's data-plane counterpart to small
``output_values``. These tests lock in the v1 behavior: trusted backend
artifact metadata is indexed on the run, exposed through the canonical
``steps.<step_key>.artifact`` namespace, and resolvable by explicit bindings
without mutating payload or ordinary output values.
"""

from types import SimpleNamespace

import pytest
from validibot_shared.validations.envelopes import ValidationArtifact

from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepStatus
from validibot.validations.models import Artifact
from validibot.validations.services.artifacts import build_step_artifact_refs
from validibot.validations.services.artifacts import register_output_artifacts
from validibot.validations.services.path_resolution import resolve_input_signal
from validibot.validations.services.run_context import RunContextBuilder
from validibot.validations.tests.factories import StepInputBindingFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.workflows.tests.factories import WorkflowStepFactory

pytestmark = pytest.mark.django_db

UPDATED_REPORT_SIZE_BYTES = 200


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
        signal_definition = StepIODefinitionFactory(
            validator=None,
            workflow_step=downstream_step,
            direction=SignalDirection.INPUT,
            contract_key="primary_model",
            native_name="primary-model",
            data_type=CatalogValueType.ARTIFACT_REF,
            io_medium=StepIOMedium.ARTIFACT,
        )
        binding = StepInputBindingFactory(
            workflow_step=downstream_step,
            signal_definition=signal_definition,
            source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
            source_data_path=f"{upstream_step.step_key}.generated_model",
        )
        context = RunContextBuilder(run, downstream_step).build()

        resolved = resolve_input_signal(
            binding,
            upstream_steps=context.upstream_steps,
        )

        assert resolved.resolved is True
        assert resolved.value["schema_version"] == "validibot.artifact_ref.v1"
        assert resolved.value["uri"] == "gs://bucket/model.epjson"
        assert resolved.upstream_step_key == upstream_step.step_key
