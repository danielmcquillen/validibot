"""
Tests for ``build_energyplus_input_envelope()`` — the envelope builder service.

The envelope builder constructs typed Pydantic ``EnergyPlusInputEnvelope``
objects that the validator container reads as its primary input.  The envelope
encapsulates all context the container needs:

- **Validator info**: type, version, ID (for logging/tracing)
- **Org/workflow info**: used for storage paths and callback routing
- **Input files**: the primary model file (IDF or epJSON) with correct
  ``name`` and ``mime_type`` so the runner saves it with the right extension
- **Resource files**: weather files (EPW) and any other auxiliary files
- **Execution context**: callback URL, callback ID for idempotency,
  ``skip_callback`` flag for sync backends
- **EnergyPlus inputs**: ``timestep_per_hour`` and future run settings

These tests verify envelope construction with real ``ValidatorFactory``
instances (not hand-rolled mocks), ensuring the builder correctly handles
UUID fields, validation type normalization, and other real model behavior.
"""

from types import SimpleNamespace

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from validibot_shared.energyplus.envelopes import EnergyPlusInputEnvelope
from validibot_shared.shacl.envelopes import SHACLInputEnvelope
from validibot_shared.validations.envelopes import ResourceFileItem
from validibot_shared.validations.envelopes import ValidationArtifact
from validibot_shared.validations.envelopes import ValidatorType

from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.tests.factories import SubmissionFactory
from validibot.validations.constants import FMU_MODEL_RESOURCE
from validibot.validations.constants import ArtifactKind
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogValueType
from validibot.validations.constants import EnvelopeChannel
from validibot.validations.constants import ResourceFileType
from validibot.validations.constants import RulesetType
from validibot.validations.constants import SignalDirection
from validibot.validations.constants import SignalOriginKind
from validibot.validations.constants import SignalSourceKind
from validibot.validations.constants import StepIOMedium
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import Artifact
from validibot.validations.models import FMUModel
from validibot.validations.models import ResolvedInputTrace
from validibot.validations.services.artifacts import register_output_artifacts
from validibot.validations.services.cloud_run.envelope_builder import (
    build_energyplus_input_envelope,
)
from validibot.validations.services.cloud_run.envelope_builder import (
    build_input_envelope,
)
from validibot.validations.tests.factories import RulesetFactory
from validibot.validations.tests.factories import StepInputBindingFactory
from validibot.validations.tests.factories import StepIODefinitionFactory
from validibot.validations.tests.factories import ValidationRunFactory
from validibot.validations.tests.factories import ValidationStepRunFactory
from validibot.validations.tests.factories import ValidatorFactory
from validibot.validations.tests.factories import ValidatorResourceFileFactory
from validibot.workflows.models import WorkflowStepResource
from validibot.workflows.tests.factories import WorkflowStepFactory
from validibot.workflows.tests.factories import WorkflowStepResourceFactory

pytestmark = pytest.mark.django_db


# ==============================================================================
# Helpers
# ==============================================================================


def _make_weather_resource(
    uri: str = "gs://test-bucket/weather.epw",
) -> ResourceFileItem:
    """Create a ResourceFileItem for a weather file.

    Weather files are the most common resource type attached to EnergyPlus
    envelopes.  They're passed as ``resource_files`` (not ``input_files``)
    because the runner downloads them separately from the model file.
    """
    return ResourceFileItem(
        id="resource-weather-123",
        type="energyplus_weather",
        uri=uri,
    )


def _build_envelope(validator=None, **overrides) -> EnergyPlusInputEnvelope:
    """Build an envelope with sensible defaults, allowing per-test overrides.

    Reduces boilerplate across tests — each test only specifies the
    parameters it cares about.
    """
    if validator is None:
        validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)

    defaults = {
        "run_id": "run-123",
        "validator": validator,
        "org_id": "org-456",
        "org_name": "Test Organization",
        "workflow_id": "workflow-789",
        "step_id": "step-012",
        "step_name": "EnergyPlus Simulation",
        "model_file_uri": "gs://test-bucket/model.idf",
        "resource_files": [_make_weather_resource()],
        "callback_url": "https://api.example.com/callbacks/",
        "callback_id": "cb-test-123",
        "execution_bundle_uri": "gs://test-bucket/runs/run-123/",
    }
    defaults.update(overrides)
    return build_energyplus_input_envelope(**defaults)


def _build_fmu_run(*, submission_content: str = "{}"):
    """Create a runnable FMU step graph for envelope-builder tests."""
    validator = ValidatorFactory(validation_type=ValidationType.FMU)
    step = WorkflowStepFactory(validator=validator)
    WorkflowStepResourceFactory(
        step=step,
        role=WorkflowStepResource.FMU_MODEL,
        validator_resource_file=None,
        step_resource_file=SimpleUploadedFile("model.fmu", b"fmu-bytes"),
        filename="model.fmu",
        resource_type="fmu",
    )
    submission = SubmissionFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        content=submission_content,
    )
    run = ValidationRunFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        submission=submission,
    )
    ValidationStepRunFactory(
        validation_run=run,
        workflow_step=step,
        step_order=step.order,
    )
    return run, step


def _make_fmu_model_port(validator):
    """Create the declared FMU model artifact input port for envelope tests."""

    return StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key="fmu_model",
        native_name="fmu_model",
        direction=SignalDirection.INPUT,
        origin_kind=SignalOriginKind.CATALOG,
        source_kind=SignalSourceKind.PAYLOAD_PATH,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        media_type="application/vnd.fmi.fmu",
        data_format=SubmissionDataFormat.FMU,
        accepted_data_formats=[SubmissionDataFormat.FMU],
        accepted_media_types=["application/vnd.fmi.fmu"],
        metadata={"accepted_extensions": ["fmu"]},
        envelope_channel=EnvelopeChannel.INPUT_FILES,
        resource_type=FMU_MODEL_RESOURCE,
        role="fmu",
        min_items=1,
        max_items=1,
        allowed_source_scopes=[
            BindingSourceScope.WORKFLOW_RESOURCE,
            BindingSourceScope.SYSTEM,
        ],
    )


def _make_shacl_data_graph_port(validator):
    """Create the declared SHACL data graph artifact input port for tests."""

    return StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key="data_graph",
        native_name="data_graph",
        direction=SignalDirection.INPUT,
        origin_kind=SignalOriginKind.CATALOG,
        source_kind=SignalSourceKind.PAYLOAD_PATH,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        media_type="text/turtle",
        data_format=SubmissionDataFormat.TEXT,
        accepted_data_formats=[
            SubmissionDataFormat.TEXT,
            SubmissionDataFormat.JSON,
            SubmissionDataFormat.XML,
        ],
        accepted_media_types=[
            "text/turtle",
            "application/rdf+xml",
            "application/ld+json",
            "application/n-triples",
            "application/n-quads",
        ],
        metadata={"accepted_extensions": ["ttl", "rdf", "jsonld", "nt", "nq"]},
        envelope_channel=EnvelopeChannel.INPUT_FILES,
        role="data-graph",
        min_items=1,
        max_items=1,
        allowed_source_scopes=[
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
    )


def _build_shacl_data_graph_run(
    *,
    original_filename: str = "submission.ttl",
    file_type: str = SubmissionFileType.TEXT,
):
    """Create a SHACL run with a declared ``data_graph`` artifact input port."""

    validator = ValidatorFactory(
        validation_type=ValidationType.SHACL,
        version=3,
    )
    ruleset = RulesetFactory(
        org=validator.org,
        ruleset_type=RulesetType.SHACL,
        rules_text="@prefix sh: <http://www.w3.org/ns/shacl#> .",
        metadata={"submission_format": "auto"},
    )
    step = WorkflowStepFactory(
        validator=validator,
        name="Validate RDF",
        ruleset=ruleset,
    )
    submission = SubmissionFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        content="@prefix ex: <http://example.org/> . ex:a a ex:Thing .",
        file_type=file_type,
        original_filename=original_filename,
    )
    run = ValidationRunFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        submission=submission,
    )
    ValidationStepRunFactory(
        validation_run=run,
        workflow_step=step,
        step_order=step.order,
        status=StepStatus.PENDING,
    )
    data_graph_port = _make_shacl_data_graph_port(validator)
    return run, step, data_graph_port


def _build_energyplus_file_port_run():
    """Create an EnergyPlus run with declared model/weather artifact ports.

    The helper mirrors the post-``sync_validators`` shape: file ports are
    validator-owned ``StepIODefinition`` rows and per-step bindings decide where
    each file comes from.
    """
    validator = ValidatorFactory(validation_type=ValidationType.ENERGYPLUS)
    step = WorkflowStepFactory(
        validator=validator,
        name="Run Simulation",
        config={"timestep_per_hour": 6},
    )
    submission = SubmissionFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        content="Version,25.1;",
    )
    run = ValidationRunFactory(
        workflow=step.workflow,
        org=step.workflow.org,
        submission=submission,
    )
    ValidationStepRunFactory(
        validation_run=run,
        workflow_step=step,
        step_order=step.order,
        status=StepStatus.PENDING,
    )

    primary_port = StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key="primary_model",
        native_name="primary_model",
        direction=SignalDirection.INPUT,
        origin_kind=SignalOriginKind.CATALOG,
        source_kind=SignalSourceKind.PAYLOAD_PATH,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        media_type="application/vnd.energyplus.idf",
        data_format=SubmissionDataFormat.ENERGYPLUS_IDF,
        accepted_data_formats=[
            SubmissionDataFormat.ENERGYPLUS_IDF,
            SubmissionDataFormat.ENERGYPLUS_EPJSON,
        ],
        accepted_media_types=[
            "application/vnd.energyplus.idf",
            "application/vnd.energyplus.epjson",
        ],
        metadata={"accepted_extensions": ["idf", "epjson", "json"]},
        envelope_channel=EnvelopeChannel.INPUT_FILES,
        role="primary-model",
        min_items=1,
        max_items=1,
        allowed_source_scopes=[
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
    )
    weather_port = StepIODefinitionFactory(
        validator=validator,
        workflow_step=None,
        contract_key="weather_file",
        native_name="weather_file",
        direction=SignalDirection.INPUT,
        origin_kind=SignalOriginKind.CATALOG,
        source_kind=SignalSourceKind.PAYLOAD_PATH,
        data_type=CatalogValueType.ARTIFACT_REF,
        io_medium=StepIOMedium.ARTIFACT,
        artifact_kind=ArtifactKind.FILE,
        media_type="application/vnd.energyplus.epw",
        data_format=ResourceFileType.ENERGYPLUS_WEATHER,
        accepted_data_formats=[ResourceFileType.ENERGYPLUS_WEATHER],
        accepted_media_types=["application/vnd.energyplus.epw"],
        metadata={"accepted_extensions": ["epw"]},
        envelope_channel=EnvelopeChannel.RESOURCE_FILES,
        resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        role="weather",
        min_items=1,
        max_items=1,
        allowed_source_scopes=[
            BindingSourceScope.WORKFLOW_RESOURCE,
            BindingSourceScope.SUBMISSION_FILE,
            BindingSourceScope.UPSTREAM_ARTIFACT,
        ],
    )
    weather_resource = WorkflowStepResourceFactory(
        step=step,
        role=WorkflowStepResource.WEATHER_FILE,
        validator_resource_file=ValidatorResourceFileFactory(
            validator=validator,
            resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
        ),
    )
    return run, step, primary_port, weather_port, weather_resource


# ==============================================================================
# Envelope structure — verifies all sections are populated correctly
# ==============================================================================
# The envelope is the contract between Django and the validator container.
# If any section is missing or malformed, the runner will fail to parse it
# and the validation job will crash without producing results.
# ==============================================================================


class TestEnvelopeStructure:
    """Tests verifying the overall envelope structure and field mapping."""

    def test_creates_correct_envelope_type(self):
        """The builder should return an ``EnergyPlusInputEnvelope`` instance.

        The shared library defines multiple envelope types (EnergyPlus, FMU).
        Using the wrong type would cause the runner's deserializer to fail.
        """
        envelope = _build_envelope()
        assert isinstance(envelope, EnergyPlusInputEnvelope)

    def test_run_id_preserved(self):
        """The ``run_id`` field should be passed through unchanged.

        The container uses this for logging and as part of the callback
        payload so Django can match results to the originating run.
        """
        envelope = _build_envelope(run_id="run-abc-123")
        assert envelope.run_id == "run-abc-123"

    def test_validator_info_from_real_model(self):
        """Validator info should be populated from the real Django model.

        The builder reads ``.id``, ``.validation_type``, and ``.version``
        from the model instance.  Using a real ``ValidatorFactory`` ensures
        UUID serialization and enum-to-string conversion work correctly.
        """
        validator = ValidatorFactory(
            validation_type=ValidationType.ENERGYPLUS,
            version=3,
        )
        envelope = _build_envelope(validator=validator)

        assert envelope.validator.id == str(validator.id)
        assert envelope.validator.type == ValidatorType.ENERGYPLUS
        assert envelope.validator.version == "3"

    def test_org_info(self):
        """Organization fields should be populated in the envelope.

        The container uses org info for storage path construction and
        logging — it needs to know which org's data it's processing.
        """
        envelope = _build_envelope(org_id="org-456", org_name="Test Organization")
        assert envelope.org.id == "org-456"
        assert envelope.org.name == "Test Organization"

    def test_workflow_info(self):
        """Workflow and step info should be populated in the envelope.

        The step name is shown in container logs for debugging which step
        of a multi-step workflow is running.
        """
        envelope = _build_envelope(
            workflow_id="workflow-789",
            step_id="step-012",
            step_name="EnergyPlus Simulation",
        )
        assert envelope.workflow.id == "workflow-789"
        assert envelope.workflow.step_id == "step-012"
        assert envelope.workflow.step_name == "EnergyPlus Simulation"

    def test_model_file_in_input_files(self):
        """The primary model file should appear in ``input_files``.

        Only the model file goes in ``input_files``; weather and other
        auxiliary files go in ``resource_files``.  The runner treats
        ``input_files[0]`` as the primary model to simulate.
        """
        envelope = _build_envelope(
            model_file_uri="gs://test-bucket/model.idf",
        )
        assert len(envelope.input_files) == 1
        model_file = envelope.input_files[0]
        assert model_file.uri == "gs://test-bucket/model.idf"
        assert model_file.role == "primary-model"

    def test_weather_resource_in_resource_files(self):
        """Weather files should appear in ``resource_files``.

        The runner downloads resource files to a working directory alongside
        the model.  Weather file URIs may be ``gs://`` (GCP) or ``file://``
        (Docker Compose local dev).
        """
        weather = _make_weather_resource(uri="gs://test-bucket/weather.epw")
        envelope = _build_envelope(resource_files=[weather])

        assert len(envelope.resource_files) == 1
        assert envelope.resource_files[0].type == "energyplus_weather"
        assert envelope.resource_files[0].uri == "gs://test-bucket/weather.epw"

    def test_execution_context(self):
        """The execution context should carry callback info and bundle URI.

        The callback URL is where the container POSTs its output envelope
        when done.  The execution bundle URI is the directory where all
        run artifacts (input, output, logs) are stored.
        """
        envelope = _build_envelope(
            callback_url="https://api.example.com/callbacks/",
            execution_bundle_uri="gs://test-bucket/runs/run-123/",
        )
        assert (
            str(envelope.context.callback_url) == "https://api.example.com/callbacks/"
        )
        assert envelope.context.execution_bundle_uri == "gs://test-bucket/runs/run-123/"

    def test_timestep_per_hour_default(self):
        """The default ``timestep_per_hour`` should be 4.

        EnergyPlus defaults to 6, but we use 4 for faster simulations
        in the common case.  Authors can override via step config.
        """
        envelope = _build_envelope()  # No timestep_per_hour override
        assert envelope.inputs.timestep_per_hour == 4  # noqa: PLR2004

    def test_timestep_per_hour_custom(self):
        """Custom ``timestep_per_hour`` values should be passed through."""
        envelope = _build_envelope(timestep_per_hour=12)
        assert envelope.inputs.timestep_per_hour == 12  # noqa: PLR2004


# ==============================================================================
# Callback ID — idempotency support for async backends
# ==============================================================================
# The callback_id enables idempotent callback processing.  When a container
# retries its POST (e.g., due to network timeout), the callback handler
# uses the ID to detect duplicates and skip reprocessing.
# ==============================================================================


class TestCallbackId:
    """Tests for callback ID handling in the envelope builder."""

    def test_callback_id_included_when_provided(self):
        """When a callback ID is provided, it should appear in the context.

        Async backends (GCP Cloud Run) always provide a callback ID
        for idempotent processing.  The container includes it in the
        callback POST so Django can detect duplicate deliveries.
        """
        envelope = _build_envelope(callback_id="cb-uuid-12345")
        assert envelope.context.callback_id == "cb-uuid-12345"

    def test_callback_id_none_for_sync_backends(self):
        """When callback_id is None, the context should accept it.

        Sync backends (Docker Compose) don't use callbacks — the processor
        reads the output envelope directly.  Passing ``None`` should not
        raise an error.
        """
        envelope = _build_envelope(callback_id=None)
        assert envelope.context.callback_id is None


# ==============================================================================
# Multiple resource files
# ==============================================================================


class TestMultipleResourceFiles:
    """Tests for envelopes with multiple resource files."""

    def test_multiple_resource_files_preserved(self):
        """All resource files should appear in the envelope, in order.

        While weather files are the most common, some validators need
        additional auxiliary files (e.g., library data, schedule files).
        The builder should pass them through without filtering.
        """
        weather = _make_weather_resource()
        library = ResourceFileItem(
            id="resource-lib-456",
            type="energyplus_library",
            uri="gs://test-bucket/library.dat",
        )
        envelope = _build_envelope(resource_files=[weather, library])

        assert len(envelope.resource_files) == 2  # noqa: PLR2004
        assert envelope.resource_files[0].type == "energyplus_weather"
        assert envelope.resource_files[1].type == "energyplus_library"


# ==============================================================================
# EnergyPlus file-port materialization
# ==============================================================================
# Declared artifact ports are the workflow-engine contract; the backend envelope
# remains the wire protocol.  These tests prove that the launch builder bridges
# those layers without reintroducing hard-coded config-only file handling.
# ==============================================================================


class TestEnergyPlusFilePortMaterialization:
    """Tests for declared EnergyPlus artifact ports in ``build_input_envelope()``."""

    def test_submitted_model_and_workflow_weather_resource_materialize(self):
        """Default file-port bindings should produce backend envelope items.

        The primary model is a submitted runtime file and the weather file is a
        workflow resource.  The envelope keeps the existing backend shape while
        adding ``port_key`` so the item is traceable to the declared contract.
        """
        run, _step, primary_port, weather_port, weather_resource = (
            _build_energyplus_file_port_run()
        )
        StepInputBindingFactory(
            workflow_step=_step,
            signal_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=_step,
            signal_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        envelope = build_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={
                "primary_file_uri": "file:///validibot/input/model.idf",
            },
            resource_uri_overrides={
                str(weather_resource.validator_resource_file_id): (
                    "file:///validibot/input/resources/weather.epw"
                ),
            },
        )

        assert len(envelope.input_files) == 1
        assert envelope.input_files[0].port_key == "primary_model"
        assert envelope.input_files[0].role == "primary-model"
        assert envelope.input_files[0].uri == "file:///validibot/input/model.idf"
        assert len(envelope.resource_files) == 1
        assert envelope.resource_files[0].port_key == "weather_file"
        assert envelope.resource_files[0].type == ResourceFileType.ENERGYPLUS_WEATHER
        assert envelope.resource_files[0].uri.endswith("/weather.epw")
        assert envelope.inputs.timestep_per_hour == 6  # noqa: PLR2004

    def test_upstream_model_artifact_materializes_as_primary_input_file(self):
        """An upstream ArtifactRef can satisfy the primary model file port.

        This guards the handoff between artifact references and file-port
        materialization, which is the core compatibility point with the
        cross-step data binding ADR.
        """
        run, step, primary_port, weather_port, weather_resource = (
            _build_energyplus_file_port_run()
        )
        upstream_step = WorkflowStepFactory(
            workflow=step.workflow,
            name="Build Model",
            order=step.order - 5,
        )
        upstream_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=upstream_step,
            step_order=upstream_step.order,
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
                        uri="gs://validibot/runs/run-1/model.epjson",
                        size_bytes=456,
                    ),
                ],
                raw_outputs=None,
            ),
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=primary_port,
            source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
            source_data_path=f"{upstream_step.step_key}.generated_model",
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        envelope = build_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            resource_uri_overrides={
                str(weather_resource.validator_resource_file_id): (
                    "file:///validibot/input/resources/weather.epw"
                ),
            },
        )

        assert envelope.input_files[0].port_key == "primary_model"
        assert envelope.input_files[0].name == "model.epjson"
        assert envelope.input_files[0].uri == "gs://validibot/runs/run-1/model.epjson"
        assert envelope.resource_files[0].port_key == "weather_file"

    def test_upstream_model_rejects_unaccepted_data_format_with_trace(self):
        """Upstream ArtifactRefs must satisfy the consumer port data format.

        The binding path proves where the artifact came from. The artifact
        metadata still has to match the file-port contract before dispatch.
        """
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        upstream_step = WorkflowStepFactory(
            workflow=step.workflow,
            name="Build Model",
            order=step.order - 5,
        )
        upstream_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=upstream_step,
            step_order=upstream_step.order,
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
                        uri="gs://validibot/runs/run-1/model.epjson",
                    ),
                ],
                raw_outputs=None,
            ),
        )
        Artifact.objects.filter(step_run=upstream_run).update(
            data_format=SubmissionDataFormat.CSV,
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=primary_port,
            source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
            source_data_path=f"{upstream_step.step_key}.generated_model",
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        with pytest.raises(ValueError, match="does not accept data format"):
            build_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            signal_contract_key="primary_model",
        )
        assert trace.resolved is False
        assert "csv" in trace.error_message

    def test_upstream_model_rejects_wrong_media_type_with_trace(self):
        """Explicitly wrong upstream artifact media types are not ignored.

        Generic JSON is accepted for legacy epJSON artifacts, but a clearly
        unrelated MIME type should fail even when the filename extension looks
        plausible.
        """
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        upstream_step = WorkflowStepFactory(
            workflow=step.workflow,
            name="Build Model",
            order=step.order - 5,
        )
        upstream_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=upstream_step,
            step_order=upstream_step.order,
            status=StepStatus.PASSED,
        )
        register_output_artifacts(
            step_run=upstream_run,
            output_envelope=SimpleNamespace(
                artifacts=[
                    ValidationArtifact(
                        name="model.epjson",
                        type="generated-model",
                        mime_type="application/pdf",
                        uri="gs://validibot/runs/run-1/model.epjson",
                    ),
                ],
                raw_outputs=None,
            ),
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=primary_port,
            source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
            source_data_path=f"{upstream_step.step_key}.generated_model",
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        with pytest.raises(ValueError, match="does not accept media type"):
            build_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            signal_contract_key="primary_model",
        )
        assert trace.resolved is False
        assert "application/pdf" in trace.error_message

    def test_missing_weather_resource_fails_with_port_specific_error(self):
        """A declared weather port should fail before launching without a file."""
        run, step, primary_port, weather_port, weather_resource = (
            _build_energyplus_file_port_run()
        )
        weather_resource.delete()
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        with pytest.raises(ValueError, match="weather_file"):
            build_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "primary_file_uri": "file:///validibot/input/model.idf",
                },
            )

    def test_submitted_weather_file_materializes_as_input_file(self):
        """Submitted EPW files should populate the weather artifact port.

        Managed weather resources stay in ``resource_files``. When the author
        chooses "Submitted file" for the weather port, the EPW is a launch-time
        input and must ride in ``input_files`` with the declared port key.
        """
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=weather_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="",
        )

        envelope = build_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={
                "primary_file_uri": "file:///validibot/input/model.idf",
                "weather_file": "file:///validibot/input/resources/weather.epw",
            },
        )

        assert [item.port_key for item in envelope.input_files] == [
            "primary_model",
            "weather_file",
        ]
        weather_item = envelope.input_files[1]
        assert weather_item.role == "weather"
        assert weather_item.name == "weather.epw"
        assert weather_item.uri == "file:///validibot/input/resources/weather.epw"
        assert envelope.resource_files == []

    def test_submitted_weather_file_records_artifact_input_traces(self):
        """File-port resolution should leave auditable input trace rows.

        The envelope proves what the backend receives; the trace table proves
        why that file was selected for this step. This is the bridge evidence
        and credentials need later.
        """
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=weather_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="",
        )

        build_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={
                "primary_file_uri": "file:///validibot/input/model.idf",
                "weather_file": "file:///validibot/input/resources/weather.epw",
            },
        )

        traces = {
            trace.signal_contract_key: trace
            for trace in ResolvedInputTrace.objects.filter(
                step_run=run.current_step_run,
            )
        }
        assert traces["primary_model"].resolved is True
        assert traces["primary_model"].value_snapshot["uri"].endswith("model.idf")
        assert traces["weather_file"].resolved is True
        assert traces["weather_file"].source_scope_used == (
            BindingSourceScope.SUBMISSION_FILE
        )
        assert traces["weather_file"].value_snapshot == {
            "source": BindingSourceScope.SUBMISSION_FILE,
            "port_key": "weather_file",
            "role": "weather",
            "uri": "file:///validibot/input/resources/weather.epw",
        }

    def test_missing_weather_file_records_failed_artifact_input_trace(self):
        """Missing artifact files should fail with a persisted port trace."""
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=weather_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="",
        )

        with pytest.raises(ValueError, match="weather_file"):
            build_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "primary_file_uri": "file:///validibot/input/model.idf",
                },
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            signal_contract_key="weather_file",
        )
        assert trace.resolved is False
        assert "submitted file URI" in trace.error_message

    def test_wrong_weather_extension_fails_before_dispatch_with_trace(self):
        """Wrong file formats should fail before the backend is launched."""
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=weather_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="",
        )

        with pytest.raises(ValueError, match=r"expected one of \.epw"):
            build_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "primary_file_uri": "file:///validibot/input/model.idf",
                    "weather_file": "file:///validibot/input/resources/weather.txt",
                },
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            signal_contract_key="weather_file",
        )
        assert trace.resolved is False
        assert "expected one of .epw" in trace.error_message

    def test_disallowed_artifact_source_scope_fails_with_trace(self):
        """Bindings must use a source scope declared by the artifact port.

        This protects the workflow contract from accidental UI/API drift: a
        submitted file cannot satisfy a port that was narrowed to upstream
        artifacts only.
        """
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        primary_port.allowed_source_scopes = [BindingSourceScope.UPSTREAM_ARTIFACT]
        primary_port.save(update_fields=["allowed_source_scopes"])
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        with pytest.raises(ValueError, match="does not allow source scope"):
            build_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "primary_file_uri": "file:///validibot/input/model.idf",
                },
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            signal_contract_key="primary_model",
        )
        assert trace.resolved is False
        assert "does not allow source scope" in trace.error_message

    def test_submitted_model_rejects_unaccepted_data_format_with_trace(self):
        """Accepted data formats are enforced after URI resolution.

        Extension checks alone are not enough once ports declare semantic data
        formats. An epJSON file should fail when the port is narrowed to IDF.
        """
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        primary_port.accepted_data_formats = [SubmissionDataFormat.ENERGYPLUS_IDF]
        primary_port.save(update_fields=["accepted_data_formats"])
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        with pytest.raises(ValueError, match="does not accept data format"):
            build_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "primary_file_uri": "file:///validibot/input/model.epjson",
                },
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            signal_contract_key="primary_model",
        )
        assert trace.resolved is False
        assert "energyplus_epjson" in trace.error_message

    def test_submitted_model_rejects_unaccepted_media_type_with_trace(self):
        """Accepted media types are enforced separately from data formats.

        A port may accept the epJSON data format only when it is carried with
        an accepted MIME type. This prevents generic file acceptance from
        bypassing the declared backend contract.
        """
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        primary_port.accepted_media_types = ["application/vnd.energyplus.idf"]
        primary_port.save(update_fields=["accepted_media_types"])
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        with pytest.raises(ValueError, match="does not accept media type"):
            build_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "primary_file_uri": "file:///validibot/input/model.epjson",
                },
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            signal_contract_key="primary_model",
        )
        assert trace.resolved is False
        assert "application/vnd.energyplus.epjson" in trace.error_message

    def test_duplicate_weather_resources_fail_cardinality_with_trace(self):
        """Workflow resources must satisfy the port's declared cardinality.

        EnergyPlus weather accepts one EPW file. If two matching resources are
        attached, launch should fail before the backend has to guess.
        """
        run, step, primary_port, weather_port, _weather_resource = (
            _build_energyplus_file_port_run()
        )
        WorkflowStepResourceFactory(
            step=step,
            role=WorkflowStepResource.WEATHER_FILE,
            validator_resource_file=ValidatorResourceFileFactory(
                validator=step.validator,
                resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
            ),
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        with pytest.raises(ValueError, match="accepts at most 1"):
            build_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "primary_file_uri": "file:///validibot/input/model.idf",
                },
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            signal_contract_key="weather_file",
        )
        assert trace.resolved is False
        assert "accepts at most 1" in trace.error_message

    def test_workflow_weather_resource_rejects_wrong_extension_with_trace(self):
        """Managed workflow resources are validated like submitted files.

        The resource table tells us the intended type, but the dispatch URI
        still needs to match the concrete backend file contract.
        """
        run, step, primary_port, weather_port, weather_resource = (
            _build_energyplus_file_port_run()
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=primary_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="primary_file_uri",
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=weather_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=ResourceFileType.ENERGYPLUS_WEATHER,
        )

        with pytest.raises(ValueError, match=r"expected one of \.epw"):
            build_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "primary_file_uri": "file:///validibot/input/model.idf",
                },
                resource_uri_overrides={
                    str(weather_resource.validator_resource_file_id): (
                        "file:///validibot/input/resources/weather.txt"
                    ),
                },
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            signal_contract_key="weather_file",
        )
        assert trace.resolved is False
        assert "expected one of .epw" in trace.error_message


# ==============================================================================
# SHACL data-graph file-port materialization
# ==============================================================================
# SHACL historically received the submitted RDF file through ``primary_file_uri``.
# The artifact-port contract makes the conceptual input explicit as
# ``data_graph`` while preserving the same backend envelope shape.
# ==============================================================================


class TestSHACLDataGraphFilePortMaterialization:
    """Tests for resolving SHACL's RDF data graph through artifact ports."""

    def test_submitted_rdf_data_graph_materializes_with_port_key(self):
        """A submitted Turtle file should populate SHACL ``input_files``.

        The backend still receives the first input file URI, but the envelope
        now carries ``port_key=data_graph`` so traces, evidence, and future
        binding UIs can identify the semantic input instead of relying on an
        implicit SHACL-only convention.
        """
        run, step, data_graph_port = _build_shacl_data_graph_run()
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=data_graph_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="data_graph",
        )

        envelope = build_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={
                "data_graph": "file:///validibot/input/submission.ttl",
            },
        )

        assert isinstance(envelope, SHACLInputEnvelope)
        assert envelope.input_files[0].port_key == "data_graph"
        assert envelope.input_files[0].role == "data-graph"
        assert envelope.input_files[0].name == "submission.ttl"
        assert envelope.input_files[0].uri == "file:///validibot/input/submission.ttl"
        assert envelope.input_files[0].mime_type == "text/turtle"
        assert envelope.inputs.rdf_format == "turtle"

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            signal_contract_key="data_graph",
        )
        assert trace.resolved is True
        assert trace.source_scope_used == BindingSourceScope.SUBMISSION_FILE
        assert trace.value_snapshot == {
            "source": BindingSourceScope.SUBMISSION_FILE,
            "port_key": "data_graph",
            "role": "data-graph",
            "uri": "file:///validibot/input/submission.ttl",
        }

    def test_upstream_rdf_artifact_sets_auto_detected_format_from_artifact_uri(self):
        """Upstream ArtifactRefs should drive SHACL auto-format detection.

        If the original submission was Turtle but a previous step produced
        JSON-LD, the SHACL backend must parse the upstream artifact as JSON-LD.
        This pins the handoff to the artifact's URI rather than the original
        submission filename.
        """
        run, step, data_graph_port = _build_shacl_data_graph_run(
            original_filename="original.ttl",
        )
        upstream_step = WorkflowStepFactory(
            workflow=step.workflow,
            name="Build RDF",
            order=step.order - 5,
        )
        upstream_run = ValidationStepRunFactory(
            validation_run=run,
            workflow_step=upstream_step,
            step_order=upstream_step.order,
            status=StepStatus.PASSED,
        )
        register_output_artifacts(
            step_run=upstream_run,
            output_envelope=SimpleNamespace(
                artifacts=[
                    ValidationArtifact(
                        name="graph.jsonld",
                        type="data_graph",
                        mime_type="application/json",
                        uri="gs://validibot/runs/run-1/graph.jsonld",
                        size_bytes=789,
                    ),
                ],
                raw_outputs=None,
            ),
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=data_graph_port,
            source_scope=BindingSourceScope.UPSTREAM_ARTIFACT,
            source_data_path=f"{upstream_step.step_key}.data_graph",
        )

        envelope = build_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
        )

        assert envelope.input_files[0].port_key == "data_graph"
        assert envelope.input_files[0].uri == "gs://validibot/runs/run-1/graph.jsonld"
        assert envelope.input_files[0].mime_type == "application/ld+json"
        assert envelope.inputs.rdf_format == "json-ld"

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            signal_contract_key="data_graph",
        )
        assert trace.resolved is True
        assert trace.source_scope_used == BindingSourceScope.UPSTREAM_ARTIFACT
        assert trace.upstream_step_key == upstream_step.step_key

    def test_wrong_data_graph_extension_fails_before_dispatch_with_trace(self):
        """SHACL should reject files outside the declared RDF extension set."""
        run, step, data_graph_port = _build_shacl_data_graph_run()
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=data_graph_port,
            source_scope=BindingSourceScope.SUBMISSION_FILE,
            source_data_path="data_graph",
        )

        with pytest.raises(ValueError, match=r"expected one of \.ttl"):
            build_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={
                    "data_graph": "file:///validibot/input/submission.txt",
                },
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            signal_contract_key="data_graph",
        )
        assert trace.resolved is False
        assert "expected one of .ttl" in trace.error_message


# ==============================================================================
# FMU input bindings
# ==============================================================================
# FMU envelopes must receive values through explicit StepInputBinding rows.
# Passing the whole submission JSON when bindings are missing would reintroduce
# a second execution contract and hide missing author wiring.
# ==============================================================================


class TestFMUFilePortMaterialization:
    """Tests for resolving the FMU model file through artifact-port bindings."""

    def test_step_owned_fmu_model_resolves_through_artifact_port(self):
        """Step-level FMU uploads should materialize as ``fmu_model`` input files."""
        run, step = _build_fmu_run()
        fmu_port = _make_fmu_model_port(step.validator)
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=fmu_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=FMU_MODEL_RESOURCE,
        )

        envelope = build_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={"fmu_model_uri": "file:///validibot/input/model.fmu"},
        )

        assert envelope.input_files[0].port_key == "fmu_model"
        assert envelope.input_files[0].role == "fmu"
        assert envelope.input_files[0].name == "model.fmu"
        assert envelope.input_files[0].uri == "file:///validibot/input/model.fmu"

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            signal_contract_key="fmu_model",
        )
        assert trace.resolved is True
        assert trace.source_scope_used == BindingSourceScope.WORKFLOW_RESOURCE
        assert trace.value_snapshot["type"] == FMU_MODEL_RESOURCE

    def test_library_fmu_model_resolves_through_system_artifact_port(self):
        """Library FMU validators should also use the same ``fmu_model`` port."""
        fmu_model = FMUModel.objects.create(
            org=None,
            name="Library FMU",
            file=SimpleUploadedFile("library.fmu", b"fmu-bytes"),
            checksum="abc123",
            gcs_uri="gs://validibot/fmus/library.fmu",
        )
        validator = ValidatorFactory(
            validation_type=ValidationType.FMU,
            fmu_model=fmu_model,
        )
        step = WorkflowStepFactory(validator=validator)
        submission = SubmissionFactory(
            workflow=step.workflow,
            org=step.workflow.org,
            content="{}",
        )
        run = ValidationRunFactory(
            workflow=step.workflow,
            org=step.workflow.org,
            submission=submission,
        )
        ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
            step_order=step.order,
        )
        fmu_port = _make_fmu_model_port(validator)
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=fmu_port,
            source_scope=BindingSourceScope.SYSTEM,
            source_data_path="fmu_model",
        )

        envelope = build_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
        )

        assert envelope.input_files[0].port_key == "fmu_model"
        assert envelope.input_files[0].uri == "gs://validibot/fmus/library.fmu"

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            signal_contract_key="fmu_model",
        )
        assert trace.resolved is True
        assert trace.source_scope_used == BindingSourceScope.SYSTEM
        assert trace.value_snapshot["fmu_model_id"] == str(fmu_model.id)
        assert trace.value_snapshot["sha256"] == "abc123"

    def test_missing_step_owned_fmu_model_fails_with_port_trace(self):
        """Missing FMU resources should fail before dispatch with a port trace."""
        validator = ValidatorFactory(validation_type=ValidationType.FMU)
        step = WorkflowStepFactory(validator=validator)
        submission = SubmissionFactory(
            workflow=step.workflow,
            org=step.workflow.org,
            content="{}",
        )
        run = ValidationRunFactory(
            workflow=step.workflow,
            org=step.workflow.org,
            submission=submission,
        )
        ValidationStepRunFactory(
            validation_run=run,
            workflow_step=step,
            step_order=step.order,
        )
        fmu_port = _make_fmu_model_port(validator)
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=fmu_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=FMU_MODEL_RESOURCE,
        )

        with pytest.raises(ValueError, match="fmu_model"):
            build_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            signal_contract_key="fmu_model",
        )
        assert trace.resolved is False
        assert "expected at least 1" in trace.error_message

    def test_step_owned_fmu_model_rejects_wrong_extension_with_trace(self):
        """The FMU file-port contract should reject non-FMU filenames."""
        run, step = _build_fmu_run()
        fmu_port = _make_fmu_model_port(step.validator)
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=fmu_port,
            source_scope=BindingSourceScope.WORKFLOW_RESOURCE,
            source_data_path=FMU_MODEL_RESOURCE,
        )

        with pytest.raises(ValueError, match=r"expected one of \.fmu"):
            build_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={"fmu_model_uri": "file:///validibot/input/model.txt"},
            )

        trace = ResolvedInputTrace.objects.get(
            step_run=run.current_step_run,
            signal_contract_key="fmu_model",
        )
        assert trace.resolved is False
        assert "expected one of .fmu" in trace.error_message


class TestFMUInputBindings:
    """Tests for FMU input-value construction in ``build_input_envelope()``."""

    def test_no_declared_fmu_inputs_produces_empty_input_values(self):
        """A step with no declared FMU inputs should launch with an empty map."""
        run, _step = _build_fmu_run(
            submission_content='{"accidental": "must-not-enter-envelope"}',
        )

        envelope = build_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={"fmu_model_uri": "file:///validibot/input/model.fmu"},
        )

        assert envelope.inputs.input_values == {}

    def test_declared_fmu_input_without_binding_fails_closed(self):
        """Declared inputs require bindings; raw submission JSON is not a fallback."""
        run, step = _build_fmu_run(submission_content='{"panel_area": 150.0}')
        StepIODefinitionFactory(
            workflow_step=step,
            validator=None,
            contract_key="panel_area",
            native_name="Panel.Area",
            direction=SignalDirection.INPUT,
            origin_kind=SignalOriginKind.FMU,
        )

        with pytest.raises(ValueError, match="StepInputBinding"):
            build_input_envelope(
                run,
                callback_url="http://localhost/callback/",
                callback_id=None,
                execution_bundle_uri="file:///validibot/output",
                input_file_uris={"fmu_model_uri": "file:///validibot/input/model.fmu"},
            )

    def test_declared_fmu_input_uses_binding_not_entire_submission(self):
        """Only bound values should reach envelope and canonical step state."""
        run, step = _build_fmu_run(
            submission_content=(
                '{"building": {"panel_area": 150.0}, '
                '"accidental": "must-not-enter-envelope"}'
            ),
        )
        signal = StepIODefinitionFactory(
            workflow_step=step,
            validator=None,
            contract_key="panel_area",
            native_name="Panel.Area",
            direction=SignalDirection.INPUT,
            origin_kind=SignalOriginKind.FMU,
        )
        StepInputBindingFactory(
            workflow_step=step,
            signal_definition=signal,
            source_data_path="building.panel_area",
        )

        envelope = build_input_envelope(
            run,
            callback_url="http://localhost/callback/",
            callback_id=None,
            execution_bundle_uri="file:///validibot/output",
            input_file_uris={"fmu_model_uri": "file:///validibot/input/model.fmu"},
        )

        assert envelope.inputs.input_values == {"Panel.Area": 150.0}
        step_run = run.step_runs.get(workflow_step=step)
        assert step_run.input_values == {"panel_area": 150.0}
        assert step_run.output["resolved_inputs"] == {"Panel.Area": 150.0}
