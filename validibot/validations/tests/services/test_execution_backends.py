"""
Tests for ExecutionBackend implementations.

These tests verify the behavior of the execution backend abstraction layer
that routes validations to different infrastructure targets.
"""

from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from validibot.validations.services.execution.base import ExecutionRequest
from validibot.validations.services.execution.base import ExecutionResponse
from validibot.validations.services.execution.docker_compose import (
    DockerComposeExecutionBackend,
)
from validibot.validations.services.execution.registry import clear_backend_cache
from validibot.validations.services.execution.registry import get_execution_backend

# ==============================================================================
# Mock Models for Testing
# ==============================================================================


class MockOrg:
    """Mock organization for testing."""

    def __init__(self):
        self.id = "org-test-123"
        self.name = "Test Organization"


class MockValidator:
    """Mock validator for testing."""

    def __init__(self):
        self.id = "validator-test-456"
        self.validation_type = "ENERGYPLUS"
        self.version = "24.2.0"


class MockSubmission:
    """Mock submission for testing."""

    def __init__(self):
        self.id = "submission-test-789"
        self.original_filename = "test.idf"

    def get_content(self) -> bytes:
        return b"Version,24.1;"


class MockWorkflowStep:
    """Mock workflow step for testing."""

    def __init__(self):
        self.id = "step-test-012"
        self.name = "Test Step"
        self.config = {
            "primary_file_uri": "file:///test/model.idf",
            "resource_file_ids": ["resource-weather-123"],
        }
        self.validator = MockValidator()


class MockStepRun:
    """Mock step run for testing."""

    def __init__(self, workflow_step):
        self.id = "step-run-test-345"
        self.workflow_step = workflow_step
        self.output = {}


class MockWorkflow:
    """Mock workflow for testing."""

    def __init__(self):
        self.id = "workflow-test-678"


class MockValidationRun:
    """Mock validation run for testing."""

    def __init__(self):
        self.id = "run-test-901"
        self.org = MockOrg()
        self.workflow = MockWorkflow()
        self._step = MockWorkflowStep()
        self._step_run = MockStepRun(self._step)

    @property
    def current_step_run(self):
        return self._step_run


# ==============================================================================
# ExecutionRequest Tests
# ==============================================================================


class TestExecutionRequest:
    """Tests for ExecutionRequest dataclass."""

    def test_request_properties(self):
        """Test that ExecutionRequest exposes correct properties."""
        run = MockValidationRun()
        validator = MockValidator()
        submission = MockSubmission()
        step = MockWorkflowStep()

        request = ExecutionRequest(
            run=run,
            validator=validator,
            submission=submission,
            step=step,
        )

        assert request.run_id == str(run.id)
        assert request.org_id == str(run.org.id)
        assert request.validator_type == "energyplus"


# ==============================================================================
# ExecutionResponse Tests
# ==============================================================================


class TestExecutionResponse:
    """Tests for ExecutionResponse dataclass."""

    def test_response_complete(self):
        """Test ExecutionResponse for completed execution."""
        response = ExecutionResponse(
            execution_id="exec-123",
            is_complete=True,
            output_envelope=None,
            error_message="Test error",
            duration_seconds=10.5,
        )

        assert response.execution_id == "exec-123"
        assert response.is_complete is True
        assert response.error_message == "Test error"
        assert response.duration_seconds == 10.5  # noqa: PLR2004

    def test_response_pending(self):
        """Test ExecutionResponse for pending async execution."""
        response = ExecutionResponse(
            execution_id="exec-456",
            is_complete=False,
        )

        assert response.execution_id == "exec-456"
        assert response.is_complete is False
        assert response.output_envelope is None
        assert response.error_message is None


# ==============================================================================
# Registry Tests
# ==============================================================================


class TestBackendFactory:
    """Tests for execution backend factory function."""

    @pytest.fixture(autouse=True)
    def clear_cache(self):
        """Clear backend cache before each test."""
        clear_backend_cache()
        yield
        clear_backend_cache()

    def test_deployment_target_test_uses_docker(self, settings):
        """Test that DEPLOYMENT_TARGET=test uses Docker backend."""
        settings.VALIDATOR_RUNNER = None
        settings.DEPLOYMENT_TARGET = "test"

        backend = get_execution_backend()

        assert isinstance(backend, DockerComposeExecutionBackend)
        assert backend.is_async is False

    def test_deployment_target_docker_compose_uses_docker(self, settings):
        """Test that DEPLOYMENT_TARGET=docker_compose uses Docker backend."""
        settings.VALIDATOR_RUNNER = None
        settings.DEPLOYMENT_TARGET = "docker_compose"

        backend = get_execution_backend()

        assert isinstance(backend, DockerComposeExecutionBackend)

    def test_validator_runner_overrides_deployment_target(self, settings):
        """Test that VALIDATOR_RUNNER setting overrides DEPLOYMENT_TARGET."""
        settings.VALIDATOR_RUNNER = "docker"
        settings.DEPLOYMENT_TARGET = "gcp"

        backend = get_execution_backend()

        # Should use docker (from VALIDATOR_RUNNER) not GCP (from DEPLOYMENT_TARGET)
        assert isinstance(backend, DockerComposeExecutionBackend)

    def test_backend_instance_is_cached(self, settings):
        """Test that backend instance is cached."""
        settings.VALIDATOR_RUNNER = "docker"

        backend1 = get_execution_backend()
        backend2 = get_execution_backend()

        assert backend1 is backend2

    def test_unknown_validator_runner_raises(self, settings):
        """Test that unknown VALIDATOR_RUNNER raises ValueError."""
        settings.VALIDATOR_RUNNER = "unknown_backend"
        clear_backend_cache()

        with pytest.raises(ValueError, match="Unknown VALIDATOR_RUNNER"):
            get_execution_backend()


# ==============================================================================
# DockerComposeExecutionBackend Tests
# ==============================================================================


class TestDockerComposeExecutionBackend:
    """Tests for DockerComposeExecutionBackend."""

    def test_is_async_false(self):
        """Test that Docker Compose backend is synchronous."""
        backend = DockerComposeExecutionBackend()
        assert backend.is_async is False

    def test_backend_name(self):
        """Test backend name property."""
        backend = DockerComposeExecutionBackend()
        assert backend.backend_name == "DockerComposeExecutionBackend"

    @patch("validibot.validations.services.execution.docker_compose.get_validator_runner")
    def test_is_available_true(self, mock_get_runner):
        """Test is_available returns True when Docker is available."""
        mock_runner = MagicMock()
        mock_runner.is_available.return_value = True
        mock_get_runner.return_value = mock_runner

        backend = DockerComposeExecutionBackend()
        assert backend.is_available() is True

    @patch("validibot.validations.services.execution.docker_compose.get_validator_runner")
    def test_is_available_false(self, mock_get_runner):
        """Test is_available returns False when Docker is not available."""
        mock_runner = MagicMock()
        mock_runner.is_available.return_value = False
        mock_get_runner.return_value = mock_runner

        backend = DockerComposeExecutionBackend()
        assert backend.is_available() is False

    def test_get_container_image_default(self, settings):
        """Test default container image naming convention."""
        settings.VALIDATOR_IMAGE_TAG = "latest"
        settings.VALIDATOR_IMAGE_REGISTRY = ""
        settings.VALIDATOR_IMAGES = {}

        backend = DockerComposeExecutionBackend()
        image = backend.get_container_image("energyplus")

        assert image == "validibot-validator-energyplus:latest"

    def test_get_container_image_with_registry(self, settings):
        """Test container image with registry prefix."""
        settings.VALIDATOR_IMAGE_TAG = "v1.0.0"
        settings.VALIDATOR_IMAGE_REGISTRY = "gcr.io/my-project"
        settings.VALIDATOR_IMAGES = {}

        backend = DockerComposeExecutionBackend()
        image = backend.get_container_image("fmi")

        assert image == "gcr.io/my-project/validibot-validator-fmi:v1.0.0"

    def test_get_container_image_explicit_mapping(self, settings):
        """Test explicit image mapping overrides default."""
        settings.VALIDATOR_IMAGES = {
            "energyplus": "my-custom-image:custom-tag",
        }

        backend = DockerComposeExecutionBackend()
        image = backend.get_container_image("energyplus")

        assert image == "my-custom-image:custom-tag"

    @patch("validibot.validations.services.execution.docker_compose.get_validator_runner")
    @patch("validibot.validations.services.execution.docker_compose.get_data_storage")
    def test_execute_returns_error_when_not_available(
        self, mock_get_storage, mock_get_runner
    ):
        """Test that execute returns error when Docker is not available."""
        mock_runner = MagicMock()
        mock_runner.is_available.return_value = False
        mock_get_runner.return_value = mock_runner

        backend = DockerComposeExecutionBackend()
        run = MockValidationRun()
        request = ExecutionRequest(
            run=run,
            validator=MockValidator(),
            submission=MockSubmission(),
            step=MockWorkflowStep(),
        )

        response = backend.execute(request)

        assert response.is_complete is True
        assert "not available" in response.error_message


# ==============================================================================
# skip_callback Tests
# ==============================================================================


class TestSkipCallback:
    """Tests for skip_callback functionality in envelope building."""

    def test_envelope_builder_skip_callback_true(self):
        """Test envelope builder sets skip_callback=True for sync backends."""
        from validibot_shared.validations.envelopes import ResourceFileItem

        from validibot.validations.services.cloud_run.envelope_builder import (
            build_energyplus_input_envelope,
        )

        validator = MockValidator()
        weather_resource = ResourceFileItem(
            id="resource-weather-123",
            type="energyplus_weather",
            uri="file:///test/weather.epw",
        )

        envelope = build_energyplus_input_envelope(
            run_id="run-123",
            validator=validator,
            org_id="org-456",
            org_name="Test Organization",
            workflow_id="workflow-789",
            step_id="step-012",
            step_name="Test Step",
            model_file_uri="file:///test/model.idf",
            resource_files=[weather_resource],
            callback_url="http://localhost:8000/callbacks/",
            callback_id="cb-123",
            execution_bundle_uri="file:///test/runs/123/",
            skip_callback=True,
        )

        assert envelope.context.skip_callback is True

    def test_envelope_builder_skip_callback_false(self):
        """Test envelope builder sets skip_callback=False for async backends."""
        from validibot_shared.validations.envelopes import ResourceFileItem

        from validibot.validations.services.cloud_run.envelope_builder import (
            build_energyplus_input_envelope,
        )

        validator = MockValidator()
        weather_resource = ResourceFileItem(
            id="resource-weather-123",
            type="energyplus_weather",
            uri="gs://bucket/weather.epw",
        )

        envelope = build_energyplus_input_envelope(
            run_id="run-123",
            validator=validator,
            org_id="org-456",
            org_name="Test Organization",
            workflow_id="workflow-789",
            step_id="step-012",
            step_name="Test Step",
            model_file_uri="gs://bucket/model.idf",
            resource_files=[weather_resource],
            callback_url="https://api.example.com/callbacks/",
            callback_id="cb-123",
            execution_bundle_uri="gs://bucket/runs/123/",
            skip_callback=False,
        )

        assert envelope.context.skip_callback is False
