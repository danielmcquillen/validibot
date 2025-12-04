"""
Tests for envelope_builder service.
"""

from sv_shared.energyplus.envelopes import EnergyPlusInputEnvelope

from simplevalidations.validations.services.cloud_run.envelope_builder import (
    build_energyplus_input_envelope,
)


class MockValidator:
    """Mock validator for testing."""

    def __init__(self):
        self.id = "validator-123"
        self.type = "energyplus"
        self.version = "24.2.0"


def test_build_energyplus_input_envelope():
    """Test that envelope builder creates correct structure."""
    validator = MockValidator()

    envelope = build_energyplus_input_envelope(
        run_id="run-123",
        validator=validator,
        org_id="org-456",
        org_name="Test Organization",
        workflow_id="workflow-789",
        step_id="step-012",
        step_name="EnergyPlus Simulation",
        model_file_uri="gs://test-bucket/model.idf",
        weather_file_uri="gs://test-bucket/weather.epw",
        callback_url="https://api.example.com/callbacks/",
        callback_token="test-token",  # noqa: S106
        execution_bundle_uri="gs://test-bucket/runs/run-123/",
        timestep_per_hour=4,
        output_variables=["Zone Mean Air Temperature"],
    )

    # Verify it's the right type
    assert isinstance(envelope, EnergyPlusInputEnvelope)

    # Verify basic fields
    assert envelope.run_id == "run-123"

    # Verify validator info
    assert envelope.validator.id == "validator-123"
    assert envelope.validator.type == "energyplus"
    assert envelope.validator.version == "24.2.0"

    # Verify organization info
    assert envelope.org.id == "org-456"
    assert envelope.org.name == "Test Organization"

    # Verify workflow info
    assert envelope.workflow.id == "workflow-789"
    assert envelope.workflow.step_id == "step-012"
    assert envelope.workflow.step_name == "EnergyPlus Simulation"

    # Verify input files
    assert len(envelope.input_files) == 2  # noqa: PLR2004
    model_file = envelope.input_files[0]
    assert model_file.uri == "gs://test-bucket/model.idf"
    assert model_file.role == "primary-model"
    weather_file = envelope.input_files[1]
    assert weather_file.uri == "gs://test-bucket/weather.epw"
    assert weather_file.role == "weather"

    # Verify execution context
    assert str(envelope.context.callback_url) == "https://api.example.com/callbacks/"
    assert envelope.context.callback_token == "test-token"  # noqa: S105
    assert envelope.context.execution_bundle_uri == "gs://test-bucket/runs/run-123/"

    # Verify EnergyPlus-specific inputs
    assert envelope.inputs.timestep_per_hour == 4  # noqa: PLR2004
    assert envelope.inputs.output_variables == ["Zone Mean Air Temperature"]


def test_build_energyplus_input_envelope_defaults():
    """Test envelope builder with default values."""
    validator = MockValidator()

    envelope = build_energyplus_input_envelope(
        run_id="run-123",
        validator=validator,
        org_id="org-456",
        org_name="Test Organization",
        workflow_id="workflow-789",
        step_id="step-012",
        step_name="EnergyPlus Simulation",
        model_file_uri="gs://test-bucket/model.idf",
        weather_file_uri="gs://test-bucket/weather.epw",
        callback_url="https://api.example.com/callbacks/",
        callback_token="test-token",  # noqa: S106
        execution_bundle_uri="gs://test-bucket/runs/run-123/",
        # Use defaults for timestep_per_hour and output_variables
    )

    # Verify defaults
    assert envelope.inputs.timestep_per_hour == 4  # noqa: PLR2004
    assert envelope.inputs.output_variables == []
