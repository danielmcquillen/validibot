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

import pytest
from validibot_shared.energyplus.envelopes import EnergyPlusInputEnvelope
from validibot_shared.validations.envelopes import ResourceFileItem
from validibot_shared.validations.envelopes import ValidatorType

from validibot.validations.constants import ValidationType
from validibot.validations.services.cloud_run.envelope_builder import (
    build_energyplus_input_envelope,
)
from validibot.validations.tests.factories import ValidatorFactory

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
            version="24.2.0",
        )
        envelope = _build_envelope(validator=validator)

        assert envelope.validator.id == str(validator.id)
        assert envelope.validator.type == ValidatorType.ENERGYPLUS
        assert envelope.validator.version == "24.2.0"

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
