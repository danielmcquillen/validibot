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
from validibot.validations.services.execution.registry import _get_backend_name
from validibot.validations.services.execution.registry import clear_backend_cache
from validibot.validations.services.execution.registry import get_execution_backend
from validibot.validations.services.execution.self_hosted import (
    SelfHostedExecutionBackend,
)

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
            "weather_file_uri": "file:///test/weather.epw",
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


class TestBackendRegistry:
    """Tests for backend registry functions."""

    @pytest.fixture(autouse=True)
    def clear_cache(self):
        """Clear backend cache before each test."""
        clear_backend_cache()
        yield
        clear_backend_cache()

    def test_get_backend_name_default_docker(self, settings):
        """Test that default backend is docker when nothing is configured."""
        settings.VALIDATOR_RUNNER = None
        settings.GCP_PROJECT_ID = None

        name = _get_backend_name()
        assert name == "docker"

    def test_get_backend_name_explicit_setting(self, settings):
        """Test that explicit VALIDATOR_RUNNER setting is used."""
        settings.VALIDATOR_RUNNER = "google_cloud_run"

        name = _get_backend_name()
        assert name == "google_cloud_run"

    def test_get_backend_name_auto_detect_gcp(self, settings):
        """Test that GCP is auto-detected when GCP_PROJECT_ID is set."""
        settings.VALIDATOR_RUNNER = None
        settings.GCP_PROJECT_ID = "test-project"

        name = _get_backend_name()
        assert name == "google_cloud_run"

    def test_get_execution_backend_docker(self, settings):
        """Test that get_execution_backend returns Docker backend."""
        settings.VALIDATOR_RUNNER = "docker"

        backend = get_execution_backend()

        assert isinstance(backend, SelfHostedExecutionBackend)
        assert backend.is_async is False

    def test_get_execution_backend_cached(self, settings):
        """Test that backend instance is cached."""
        settings.VALIDATOR_RUNNER = "docker"

        backend1 = get_execution_backend()
        backend2 = get_execution_backend()

        assert backend1 is backend2

    def test_get_execution_backend_unknown_raises(self, settings):
        """Test that unknown backend raises ValueError."""
        settings.VALIDATOR_RUNNER = "unknown_backend"
        clear_backend_cache()

        with pytest.raises(ValueError, match="Unknown execution backend"):
            get_execution_backend()


# ==============================================================================
# SelfHostedExecutionBackend Tests
# ==============================================================================


class TestSelfHostedExecutionBackend:
    """Tests for SelfHostedExecutionBackend."""

    def test_is_async_false(self):
        """Test that self-hosted backend is synchronous."""
        backend = SelfHostedExecutionBackend()
        assert backend.is_async is False

    def test_backend_name(self):
        """Test backend name property."""
        backend = SelfHostedExecutionBackend()
        assert backend.backend_name == "SelfHostedExecutionBackend"

    @patch("validibot.validations.services.execution.self_hosted.get_validator_runner")
    def test_is_available_true(self, mock_get_runner):
        """Test is_available returns True when Docker is available."""
        mock_runner = MagicMock()
        mock_runner.is_available.return_value = True
        mock_get_runner.return_value = mock_runner

        backend = SelfHostedExecutionBackend()
        assert backend.is_available() is True

    @patch("validibot.validations.services.execution.self_hosted.get_validator_runner")
    def test_is_available_false(self, mock_get_runner):
        """Test is_available returns False when Docker is not available."""
        mock_runner = MagicMock()
        mock_runner.is_available.return_value = False
        mock_get_runner.return_value = mock_runner

        backend = SelfHostedExecutionBackend()
        assert backend.is_available() is False

    def test_get_container_image_default(self, settings):
        """Test default container image naming convention."""
        settings.VALIDATOR_IMAGE_TAG = "latest"
        settings.VALIDATOR_IMAGE_REGISTRY = ""
        settings.VALIDATOR_IMAGES = {}

        backend = SelfHostedExecutionBackend()
        image = backend.get_container_image("energyplus")

        assert image == "validibot-validator-energyplus:latest"

    def test_get_container_image_with_registry(self, settings):
        """Test container image with registry prefix."""
        settings.VALIDATOR_IMAGE_TAG = "v1.0.0"
        settings.VALIDATOR_IMAGE_REGISTRY = "gcr.io/my-project"
        settings.VALIDATOR_IMAGES = {}

        backend = SelfHostedExecutionBackend()
        image = backend.get_container_image("fmi")

        assert image == "gcr.io/my-project/validibot-validator-fmi:v1.0.0"

    def test_get_container_image_explicit_mapping(self, settings):
        """Test explicit image mapping overrides default."""
        settings.VALIDATOR_IMAGES = {
            "energyplus": "my-custom-image:custom-tag",
        }

        backend = SelfHostedExecutionBackend()
        image = backend.get_container_image("energyplus")

        assert image == "my-custom-image:custom-tag"

    @patch("validibot.validations.services.execution.self_hosted.get_validator_runner")
    @patch("validibot.validations.services.execution.self_hosted.get_data_storage")
    def test_execute_returns_error_when_not_available(
        self, mock_get_storage, mock_get_runner
    ):
        """Test that execute returns error when Docker is not available."""
        mock_runner = MagicMock()
        mock_runner.is_available.return_value = False
        mock_get_runner.return_value = mock_runner

        backend = SelfHostedExecutionBackend()
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
        from validibot.validations.services.cloud_run.envelope_builder import (
            build_energyplus_input_envelope,
        )

        validator = MockValidator()

        envelope = build_energyplus_input_envelope(
            run_id="run-123",
            validator=validator,
            org_id="org-456",
            org_name="Test Organization",
            workflow_id="workflow-789",
            step_id="step-012",
            step_name="Test Step",
            model_file_uri="file:///test/model.idf",
            weather_file_uri="file:///test/weather.epw",
            callback_url="http://localhost:8000/callbacks/",
            callback_id="cb-123",
            execution_bundle_uri="file:///test/runs/123/",
            skip_callback=True,
        )

        assert envelope.context.skip_callback is True

    def test_envelope_builder_skip_callback_false(self):
        """Test envelope builder sets skip_callback=False for async backends."""
        from validibot.validations.services.cloud_run.envelope_builder import (
            build_energyplus_input_envelope,
        )

        validator = MockValidator()

        envelope = build_energyplus_input_envelope(
            run_id="run-123",
            validator=validator,
            org_id="org-456",
            org_name="Test Organization",
            workflow_id="workflow-789",
            step_id="step-012",
            step_name="Test Step",
            model_file_uri="gs://bucket/model.idf",
            weather_file_uri="gs://bucket/weather.epw",
            callback_url="https://api.example.com/callbacks/",
            callback_id="cb-123",
            execution_bundle_uri="gs://bucket/runs/123/",
            skip_callback=False,
        )

        assert envelope.context.skip_callback is False
