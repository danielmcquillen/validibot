"""
Tests for check_status() on ExecutionBackend implementations.

These tests verify that the check_status() method correctly delegates to
the runner layer and maps execution status to ExecutionResponse.
"""

from unittest.mock import MagicMock
from unittest.mock import patch

from django.test import SimpleTestCase

from validibot.validations.services.execution.base import ExecutionBackend
from validibot.validations.services.execution.base import ExecutionResponse
from validibot.validations.services.execution.docker_compose import (
    DockerComposeExecutionBackend,
)
from validibot.validations.services.execution.gcp import GCPExecutionBackend
from validibot.validations.services.runners.base import ExecutionInfo
from validibot.validations.services.runners.base import ExecutionStatus


class TestBaseCheckStatus(SimpleTestCase):
    """Tests for base ExecutionBackend.check_status()."""

    def test_base_check_status_returns_none(self):
        """Base implementation should return None (no status checking)."""

        # Create a minimal concrete subclass for testing
        class MinimalBackend(ExecutionBackend):
            is_async = False

            def execute(self, request):
                pass

            def get_container_image(self, validator_type):
                return ""

        backend = MinimalBackend()
        result = backend.check_status("some-execution-id")
        assert result is None

    def test_get_execution_status_is_removed(self):
        """Verify get_execution_status() is no longer on the base class."""
        # check_status() replaced get_execution_status()
        assert hasattr(ExecutionBackend, "check_status")
        assert not hasattr(ExecutionBackend, "get_execution_status")


class TestDockerCheckStatus(SimpleTestCase):
    """Tests for DockerComposeExecutionBackend.check_status()."""

    @patch(
        "validibot.validations.services.execution.docker_compose.get_validator_runner"
    )
    def test_check_status_delegates_to_runner(self, mock_get_runner):
        """check_status() should delegate to runner.get_execution_status()."""
        mock_runner = MagicMock()
        mock_runner.get_execution_status.return_value = ExecutionInfo(
            execution_id="container-123",
            status=ExecutionStatus.SUCCEEDED,
        )
        mock_get_runner.return_value = mock_runner

        backend = DockerComposeExecutionBackend()
        result = backend.check_status("container-123")

        assert result is not None
        assert isinstance(result, ExecutionResponse)
        assert result.execution_id == "container-123"
        assert result.is_complete is True
        assert result.error_message is None
        mock_runner.get_execution_status.assert_called_once_with("container-123")

    @patch(
        "validibot.validations.services.execution.docker_compose.get_validator_runner"
    )
    def test_check_status_running_maps_to_not_complete(self, mock_get_runner):
        """A RUNNING container should map to is_complete=False."""
        mock_runner = MagicMock()
        mock_runner.get_execution_status.return_value = ExecutionInfo(
            execution_id="container-456",
            status=ExecutionStatus.RUNNING,
        )
        mock_get_runner.return_value = mock_runner

        backend = DockerComposeExecutionBackend()
        result = backend.check_status("container-456")

        assert result is not None
        assert result.is_complete is False

    @patch(
        "validibot.validations.services.execution.docker_compose.get_validator_runner"
    )
    def test_check_status_failed_maps_to_complete_with_error(self, mock_get_runner):
        """A FAILED container should map to is_complete=True with error."""
        mock_runner = MagicMock()
        mock_runner.get_execution_status.return_value = ExecutionInfo(
            execution_id="container-789",
            status=ExecutionStatus.FAILED,
            error_message="OOM killed",
        )
        mock_get_runner.return_value = mock_runner

        backend = DockerComposeExecutionBackend()
        result = backend.check_status("container-789")

        assert result is not None
        assert result.is_complete is True
        assert result.error_message == "OOM killed"

    @patch(
        "validibot.validations.services.execution.docker_compose.get_validator_runner"
    )
    def test_check_status_handles_not_found(self, mock_get_runner):
        """Returns None when the container is not found."""
        mock_runner = MagicMock()
        mock_runner.get_execution_status.side_effect = ValueError("Container not found")
        mock_get_runner.return_value = mock_runner

        backend = DockerComposeExecutionBackend()
        result = backend.check_status("nonexistent")

        assert result is None

    @patch(
        "validibot.validations.services.execution.docker_compose.get_validator_runner"
    )
    def test_check_status_handles_docker_error(self, mock_get_runner):
        """Returns None when Docker raises an unexpected error."""
        mock_runner = MagicMock()
        mock_runner.get_execution_status.side_effect = RuntimeError(
            "Docker daemon down"
        )
        mock_get_runner.return_value = mock_runner

        backend = DockerComposeExecutionBackend()
        result = backend.check_status("container-abc")

        assert result is None

    @patch(
        "validibot.validations.services.execution.docker_compose.get_validator_runner"
    )
    def test_check_status_pending_maps_to_not_complete(self, mock_get_runner):
        """A PENDING container should map to is_complete=False."""
        mock_runner = MagicMock()
        mock_runner.get_execution_status.return_value = ExecutionInfo(
            execution_id="container-pend",
            status=ExecutionStatus.PENDING,
        )
        mock_get_runner.return_value = mock_runner

        backend = DockerComposeExecutionBackend()
        result = backend.check_status("container-pend")

        assert result is not None
        assert result.is_complete is False


class TestGCPCheckStatus(SimpleTestCase):
    """Tests for GCPExecutionBackend.check_status().

    The GCP backend lazy-imports GoogleCloudRunValidatorRunner inside
    check_status(), so we patch at the source module where the class is
    defined: ``validibot.validations.services.runners.google_cloud_run``.
    """

    RUNNER_PATH = (
        "validibot.validations.services.runners.google_cloud_run"
        ".GoogleCloudRunValidatorRunner"
    )

    @patch(RUNNER_PATH)
    def test_check_status_delegates_to_runner(self, mock_runner_cls):
        """check_status() should create a runner and delegate."""
        mock_runner_instance = MagicMock()
        mock_runner_instance.get_execution_status.return_value = ExecutionInfo(
            execution_id="projects/p/locations/r/jobs/j/executions/e",
            status=ExecutionStatus.SUCCEEDED,
        )
        mock_runner_cls.return_value = mock_runner_instance

        backend = GCPExecutionBackend()
        backend._project_id = "test-project"
        backend._region = "us-central1"

        result = backend.check_status("projects/p/locations/r/jobs/j/executions/e")

        assert result is not None
        assert isinstance(result, ExecutionResponse)
        assert result.is_complete is True
        assert result.error_message is None
        mock_runner_cls.assert_called_once_with(
            project_id="test-project",
            region="us-central1",
        )

    @patch(RUNNER_PATH)
    def test_check_status_running_maps_to_not_complete(self, mock_runner_cls):
        """A RUNNING Cloud Run execution should map to is_complete=False."""
        mock_runner_instance = MagicMock()
        mock_runner_instance.get_execution_status.return_value = ExecutionInfo(
            execution_id="projects/p/locations/r/jobs/j/executions/e",
            status=ExecutionStatus.RUNNING,
        )
        mock_runner_cls.return_value = mock_runner_instance

        backend = GCPExecutionBackend()
        backend._project_id = "test-project"
        backend._region = "us-central1"

        result = backend.check_status("projects/p/locations/r/jobs/j/executions/e")

        assert result is not None
        assert result.is_complete is False

    @patch(RUNNER_PATH)
    def test_check_status_failed_maps_to_complete_with_error(self, mock_runner_cls):
        """A FAILED Cloud Run execution should map to is_complete=True."""
        mock_runner_instance = MagicMock()
        mock_runner_instance.get_execution_status.return_value = ExecutionInfo(
            execution_id="projects/p/locations/r/jobs/j/executions/e",
            status=ExecutionStatus.FAILED,
            error_message="Container OOM killed",
        )
        mock_runner_cls.return_value = mock_runner_instance

        backend = GCPExecutionBackend()
        backend._project_id = "test-project"
        backend._region = "us-central1"

        result = backend.check_status("projects/p/locations/r/jobs/j/executions/e")

        assert result is not None
        assert result.is_complete is True
        assert result.error_message == "Container OOM killed"

    def test_check_status_handles_import_error(self):
        """Returns None when google-cloud-run is not installed."""
        backend = GCPExecutionBackend()
        backend._project_id = "test-project"
        backend._region = "us-central1"

        # Temporarily remove the runner module so the lazy import fails
        import sys

        module_key = "validibot.validations.services.runners.google_cloud_run"
        saved = sys.modules.get(module_key)
        sys.modules[module_key] = None  # forces ImportError on import

        try:
            result = backend.check_status("some-execution")
        finally:
            if saved is None:
                sys.modules.pop(module_key, None)
            else:
                sys.modules[module_key] = saved

        assert result is None

    @patch(RUNNER_PATH)
    def test_check_status_handles_api_error(self, mock_runner_cls):
        """Returns None when the Cloud Run API call fails."""
        mock_runner_instance = MagicMock()
        mock_runner_instance.get_execution_status.side_effect = ValueError(
            "Execution not found"
        )
        mock_runner_cls.return_value = mock_runner_instance

        backend = GCPExecutionBackend()
        backend._project_id = "test-project"
        backend._region = "us-central1"

        result = backend.check_status("nonexistent-execution")

        assert result is None

    @patch(RUNNER_PATH)
    def test_check_status_handles_unexpected_error(self, mock_runner_cls):
        """Returns None on unexpected errors from the runner."""
        mock_runner_instance = MagicMock()
        mock_runner_instance.get_execution_status.side_effect = RuntimeError(
            "Network unreachable"
        )
        mock_runner_cls.return_value = mock_runner_instance

        backend = GCPExecutionBackend()
        backend._project_id = "test-project"
        backend._region = "us-central1"

        result = backend.check_status("some-execution")

        assert result is None
