"""Tests for validator envelope schemas."""


import pytest
from pydantic import ValidationError

from simplevalidations.validations.jobs.schemas import InputFileItem
from simplevalidations.validations.jobs.schemas import Severity
from simplevalidations.validations.jobs.schemas import SupportedMimeType
from simplevalidations.validations.jobs.schemas import ValidationCallback
from simplevalidations.validations.jobs.schemas import ValidationInputEnvelope
from simplevalidations.validations.jobs.schemas import ValidationMessage
from simplevalidations.validations.jobs.schemas import ValidationOutputEnvelope
from simplevalidations.validations.jobs.schemas import ValidationStatus
from simplevalidations.validations.jobs.schemas import ValidatorInfo


class TestInputEnvelope:
    """Tests for ValidationInputEnvelope schema."""

    def test_minimal_valid_input_envelope(self):
        """Test minimal valid input envelope."""
        data = {
            "schema_version": "validibot.input.v1",
            "run_id": "run-123",
            "validator": {
                "id": "val-123",
                "type": "energyplus",
                "version": "1.0.0",
            },
            "org": {"id": "org-123", "name": "Test Org"},
            "workflow": {"id": "wf-123", "step_id": "step-123"},
            "context": {
                "callback_url": "https://example.com/callback",
                "execution_bundle_uri": "gs://bucket/org/run",
            },
        }

        envelope = ValidationInputEnvelope.model_validate(data)
        assert envelope.run_id == "run-123"
        assert envelope.validator.type == "energyplus"
        assert envelope.org.name == "Test Org"
        assert len(envelope.input_files) == 0
        assert len(envelope.inputs) == 0
        assert envelope.context.timeout_seconds == 3600  # noqa: PLR2004

    def test_input_envelope_with_file_inputs(self):
        """Test input envelope with file inputs."""
        data = {
            "schema_version": "validibot.input.v1",
            "run_id": "run-123",
            "validator": {
                "id": "val-123",
                "type": "energyplus",
                "version": "1.0.0",
            },
            "org": {"id": "org-123", "name": "Test Org"},
            "workflow": {"id": "wf-123", "step_id": "step-123"},
            "input_files": [
                {
                    "name": "IDF File",
                    "mime_type": SupportedMimeType.ENERGYPLUS_IDF.value,
                    "role": "primary-model",
                    "uri": "gs://bucket/model.idf",
                },
                {
                    "name": "Weather File",
                    "mime_type": SupportedMimeType.ENERGYPLUS_EPW.value,
                    "role": "weather",
                    "uri": "gs://bucket/weather.epw",
                },
            ],
            "context": {
                "callback_url": "https://example.com/callback",
                "execution_bundle_uri": "gs://bucket/org/run",
                "timeout_seconds": 7200,
                "tags": ["production", "nzeb"],
            },
        }

        envelope = ValidationInputEnvelope.model_validate(data)
        assert len(envelope.input_files) == 2  # noqa: PLR2004
        assert envelope.input_files[0].role == "primary-model"
        assert envelope.input_files[1].role == "weather"
        assert envelope.context.timeout_seconds == 7200  # noqa: PLR2004
        assert "production" in envelope.context.tags

    def test_input_envelope_with_domain_config(self):
        """Test input envelope with domain-specific configuration."""
        data = {
            "schema_version": "validibot.input.v1",
            "run_id": "run-123",
            "validator": {
                "id": "val-123",
                "type": "fmu",
                "version": "1.0.0",
            },
            "org": {"id": "org-123", "name": "Test Org"},
            "workflow": {"id": "wf-123", "step_id": "step-123"},
            "inputs": {
                "timestep": 3600,
                "duration": 86400,
                "tolerance": 0.001,
            },
            "context": {
                "callback_url": "https://example.com/callback",
                "execution_bundle_uri": "gs://bucket/org/run",
            },
        }

        envelope = ValidationInputEnvelope.model_validate(data)
        assert envelope.inputs["timestep"] == 3600  # noqa: PLR2004
        assert envelope.inputs["duration"] == 86400  # noqa: PLR2004
        assert envelope.inputs["tolerance"] == 0.001  # noqa: PLR2004

    def test_input_envelope_rejects_extra_fields(self):
        """Test that input envelope rejects extra fields."""
        data = {
            "schema_version": "validibot.input.v1",
            "run_id": "run-123",
            "validator": {
                "id": "val-123",
                "type": "energyplus",
                "version": "1.0.0",
            },
            "org": {"id": "org-123", "name": "Test Org"},
            "workflow": {"id": "wf-123", "step_id": "step-123"},
            "inputs": [],
            "context": {
                "callback_url": "https://example.com/callback",
                "execution_bundle_uri": "gs://bucket/org/run",
            },
            "extra_field": "should fail",
        }

        with pytest.raises(ValidationError) as exc_info:
            ValidationInputEnvelope.model_validate(data)
        assert "Extra inputs are not permitted" in str(exc_info.value)


class TestOutputEnvelope:
    """Tests for ValidationOutputEnvelope schema."""

    def test_minimal_success_result(self):
        """Test minimal success result envelope."""
        data = {
            "schema_version": "validibot.output.v1",
            "run_id": "run-123",
            "validator": {
                "id": "val-123",
                "type": "energyplus",
                "version": "1.0.0",
            },
            "status": "success",
            "timing": {
                "started_at": "2025-12-04T10:00:00Z",
                "finished_at": "2025-12-04T10:05:00Z",
            },
        }

        envelope = ValidationOutputEnvelope.model_validate(data)
        assert envelope.run_id == "run-123"
        assert envelope.status == ValidationStatus.SUCCESS
        assert len(envelope.messages) == 0
        assert len(envelope.metrics) == 0
        assert len(envelope.artifacts) == 0

    def test_result_with_validation_messages(self):
        """Test result envelope with validation messages."""
        data = {
            "schema_version": "validibot.output.v1",
            "run_id": "run-123",
            "validator": {
                "id": "val-123",
                "type": "energyplus",
                "version": "1.0.0",
            },
            "status": "failed_validation",
            "timing": {
                "started_at": "2025-12-04T10:00:00Z",
                "finished_at": "2025-12-04T10:05:00Z",
            },
            "messages": [
                {
                    "severity": Severity.ERROR.value,
                    "code": "EP_OBJECT_REQUIRED",
                    "text": "Building object is required",
                    "location": {
                        "file_role": "primary-model",
                        "line": 1,
                        "column": 1,
                    },
                    "tags": ["syntax", "required-object"],
                },
                {
                    "severity": Severity.WARNING.value,
                    "text": "Consider adding insulation",
                    "tags": ["performance"],
                },
            ],
        }

        envelope = ValidationOutputEnvelope.model_validate(data)
        assert envelope.status == ValidationStatus.FAILED_VALIDATION
        assert len(envelope.messages) == 2  # noqa: PLR2004
        assert envelope.messages[0].severity == Severity.ERROR
        assert envelope.messages[0].code == "EP_OBJECT_REQUIRED"
        assert envelope.messages[0].location.file_role == "primary-model"
        assert envelope.messages[1].severity == Severity.WARNING

    def test_result_with_metrics(self):
        """Test result envelope with metrics."""
        data = {
            "schema_version": "validibot.output.v1",
            "run_id": "run-123",
            "validator": {
                "id": "val-123",
                "type": "energyplus",
                "version": "1.0.0",
            },
            "status": "success",
            "timing": {
                "started_at": "2025-12-04T10:00:00Z",
                "finished_at": "2025-12-04T10:05:00Z",
            },
            "metrics": [
                {
                    "name": "zone_temp_max",
                    "value": 28.5,
                    "unit": "C",
                    "category": "comfort",
                    "tags": ["Zone1"],
                },
                {
                    "name": "energy_use_intensity",
                    "value": 125.3,
                    "unit": "kWh/m2",
                    "category": "energy",
                },
            ],
        }

        envelope = ValidationOutputEnvelope.model_validate(data)
        assert len(envelope.metrics) == 2  # noqa: PLR2004
        assert envelope.metrics[0].name == "zone_temp_max"
        assert envelope.metrics[0].value == 28.5  # noqa: PLR2004
        assert envelope.metrics[0].unit == "C"
        assert envelope.metrics[1].category == "energy"

    def test_result_with_artifacts(self):
        """Test result envelope with artifacts."""
        data = {
            "schema_version": "validibot.output.v1",
            "run_id": "run-123",
            "validator": {
                "id": "val-123",
                "type": "energyplus",
                "version": "1.0.0",
            },
            "status": "success",
            "timing": {
                "started_at": "2025-12-04T10:00:00Z",
                "finished_at": "2025-12-04T10:05:00Z",
            },
            "artifacts": [
                {
                    "name": "simulation_db",
                    "type": "simulation-db",
                    "mime_type": "application/x-sqlite3",
                    "uri": "gs://bucket/org/run/outputs/simulation.sql",
                    "size_bytes": 12345678,
                },
                {
                    "name": "report",
                    "type": "report-html",
                    "mime_type": "text/html",
                    "uri": "gs://bucket/org/run/outputs/report.html",
                },
            ],
            "raw_outputs": {
                "format": "directory",
                "manifest_uri": "gs://bucket/org/run/outputs/manifest.json",
            },
        }

        envelope = ValidationOutputEnvelope.model_validate(data)
        assert len(envelope.artifacts) == 2  # noqa: PLR2004
        assert envelope.artifacts[0].name == "simulation_db"
        assert envelope.artifacts[0].size_bytes == 12345678  # noqa: PLR2004
        assert envelope.raw_outputs.format == "directory"


class TestValidationCallback:
    """Tests for ValidationCallback schema."""

    def test_valid_callback(self):
        """Test valid callback payload."""
        data = {
            "run_id": "run-123",
            "status": "success",
            "result_uri": "gs://bucket/org/run/result.json",
        }

        callback = ValidationCallback.model_validate(data)
        assert callback.run_id == "run-123"
        assert callback.status == ValidationStatus.SUCCESS
        assert "result.json" in callback.result_uri


class TestInputFileItem:
    """Tests for InputFileItem schema."""

    def test_file_input_with_uri(self):
        """Test file input with URI."""
        data = {
            "name": "Test File",
            "mime_type": "application/vnd.energyplus.idf",
            "uri": "gs://bucket/file.idf",
            "role": "primary-model",
        }
        item = InputFileItem.model_validate(data)
        assert item.name == "Test File"
        assert item.uri == "gs://bucket/file.idf"
        assert item.role == "primary-model"

    def test_file_input_minimal(self):
        """Test minimal file input."""
        data = {
            "name": "Config",
            "mime_type": "application/xml",
            "uri": "gs://bucket/config.xml",
        }
        item = InputFileItem.model_validate(data)
        assert item.name == "Config"
        assert item.role is None


class TestValidatorInfo:
    """Tests for ValidatorInfo schema."""

    def test_valid_validator_info(self):
        """Test valid validator info."""
        data = {
            "id": "val-123",
            "type": "energyplus",
            "version": "1.0.0",
        }
        info = ValidatorInfo.model_validate(data)
        assert info.type == "energyplus"
        assert info.version == "1.0.0"


class TestValidationMessage:
    """Tests for ValidationMessage schema."""

    def test_message_with_full_location(self):
        """Test message with complete location information."""
        data = {
            "severity": Severity.ERROR.value,
            "code": "TEST001",
            "text": "Test error",
            "location": {
                "file_role": "primary-model",
                "line": 42,
                "column": 10,
                "path": "Building/Zone[1]",
            },
        }
        msg = ValidationMessage.model_validate(data)
        assert msg.severity == Severity.ERROR
        assert msg.location.line == 42  # noqa: PLR2004
        assert msg.location.path == "Building/Zone[1]"

    def test_message_without_location(self):
        """Test message without location."""
        data = {
            "severity": Severity.WARNING.value,
            "text": "General warning",
        }
        msg = ValidationMessage.model_validate(data)
        assert msg.location is None
