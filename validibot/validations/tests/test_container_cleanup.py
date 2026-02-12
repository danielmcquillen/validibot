"""
Tests for container cleanup functionality (Ryuk pattern).

Tests the orphan container detection and cleanup mechanisms for
Docker Compose deployments.
"""

import sys
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from io import StringIO
from unittest.mock import MagicMock
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase


def create_mock_docker():
    """Create a mock docker module."""
    mock_docker = MagicMock()
    mock_client = MagicMock()
    mock_docker.from_env.return_value = mock_client
    return mock_docker, mock_client


def create_mock_container(
    short_id: str,
    run_id: str = "test-run",
    validator: str = "energyplus",
    started_at: datetime | None = None,
    timeout_seconds: int = 3600,
    status: str = "running",
):
    """Create a mock Docker container with Validibot labels."""
    if started_at is None:
        started_at = datetime.now(UTC)

    container = MagicMock()
    container.short_id = short_id
    container.status = status
    container.labels = {
        "org.validibot.managed": "true",
        "org.validibot.run_id": run_id,
        "org.validibot.validator": validator,
        "org.validibot.started_at": started_at.isoformat(),
        "org.validibot.timeout_seconds": str(timeout_seconds),
    }
    return container


class DockerValidatorRunnerCleanupTests(TestCase):
    """Tests for DockerValidatorRunner cleanup methods."""

    def test_list_managed_containers(self):
        """Test that list_managed_containers filters by label."""
        mock_docker, mock_client = create_mock_docker()

        mock_container = create_mock_container("abc123")
        mock_client.containers.list.return_value = [mock_container]

        with patch.dict(sys.modules, {"docker": mock_docker}):
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )

            runner = DockerValidatorRunner()
            runner._client = mock_client

            containers = runner.list_managed_containers()

            self.assertEqual(len(containers), 1)
            mock_client.containers.list.assert_called_once_with(
                all=True,
                filters={"label": "org.validibot.managed=true"},
            )

    def test_cleanup_orphaned_containers_removes_expired(self):
        """Test that cleanup removes containers past timeout + grace period."""
        mock_docker, mock_client = create_mock_docker()

        # Container started 2 hours ago with 1 hour timeout
        # Should be removed (exceeded timeout + 5 min grace)
        old_container = create_mock_container(
            short_id="old123",
            started_at=datetime.now(UTC) - timedelta(hours=2),
            timeout_seconds=3600,
        )

        # Container started 30 minutes ago with 1 hour timeout
        # Should NOT be removed
        recent_container = create_mock_container(
            short_id="new456",
            started_at=datetime.now(UTC) - timedelta(minutes=30),
            timeout_seconds=3600,
        )

        mock_client.containers.list.return_value = [old_container, recent_container]

        with patch.dict(sys.modules, {"docker": mock_docker}):
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )

            runner = DockerValidatorRunner()
            runner._client = mock_client

            removed, failed = runner.cleanup_orphaned_containers(
                grace_period_seconds=300
            )

            self.assertEqual(removed, 1)
            self.assertEqual(failed, 0)
            old_container.remove.assert_called_once_with(force=True)
            recent_container.remove.assert_not_called()

    def test_cleanup_orphaned_containers_skips_missing_label(self):
        """Test that containers without started_at label are skipped."""
        mock_docker, mock_client = create_mock_docker()

        container = MagicMock()
        container.short_id = "abc123"
        container.labels = {
            "org.validibot.managed": "true",
            # Missing started_at label
        }
        mock_client.containers.list.return_value = [container]

        with patch.dict(sys.modules, {"docker": mock_docker}):
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )

            runner = DockerValidatorRunner()
            runner._client = mock_client

            removed, failed = runner.cleanup_orphaned_containers()

            self.assertEqual(removed, 0)
            self.assertEqual(failed, 0)
            container.remove.assert_not_called()

    def test_cleanup_all_managed_containers(self):
        """Test that cleanup_all removes all managed containers."""
        mock_docker, mock_client = create_mock_docker()

        containers = [
            create_mock_container("abc123"),
            create_mock_container("def456"),
            create_mock_container("ghi789"),
        ]
        mock_client.containers.list.return_value = containers

        with patch.dict(sys.modules, {"docker": mock_docker}):
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )

            runner = DockerValidatorRunner()
            runner._client = mock_client

            removed, failed = runner.cleanup_all_managed_containers()

            self.assertEqual(removed, 3)
            self.assertEqual(failed, 0)
            for container in containers:
                container.remove.assert_called_once_with(force=True)

    def test_cleanup_handles_remove_failure(self):
        """Test that cleanup continues after a container removal failure."""
        mock_docker, mock_client = create_mock_docker()

        # First container will fail to remove
        failing_container = create_mock_container(
            short_id="fail123",
            started_at=datetime.now(UTC) - timedelta(hours=2),
        )
        failing_container.remove.side_effect = Exception("Permission denied")

        # Second container will succeed
        success_container = create_mock_container(
            short_id="ok456",
            started_at=datetime.now(UTC) - timedelta(hours=2),
        )

        mock_client.containers.list.return_value = [
            failing_container,
            success_container,
        ]

        with patch.dict(sys.modules, {"docker": mock_docker}):
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )

            runner = DockerValidatorRunner()
            runner._client = mock_client

            removed, failed = runner.cleanup_orphaned_containers(
                grace_period_seconds=300
            )

            self.assertEqual(removed, 1)
            self.assertEqual(failed, 1)


class CleanupContainersCommandTests(TestCase):
    """Tests for the cleanup_containers management command."""

    def call_command(self, *args, **kwargs):
        """Helper to call the command and capture output."""
        out = StringIO()
        err = StringIO()
        call_command(
            "cleanup_containers",
            *args,
            stdout=out,
            stderr=err,
            **kwargs,
        )
        return out.getvalue(), err.getvalue()

    def test_command_fails_when_docker_unavailable(self):
        """Test that command reports error when Docker is not available."""
        mock_docker = MagicMock()
        mock_docker.from_env.side_effect = Exception("Docker not running")

        with patch.dict(sys.modules, {"docker": mock_docker}):
            out, err = self.call_command()

            self.assertIn("Docker is not available", err)

    def test_command_runs_orphaned_cleanup(self):
        """Test that command runs orphaned cleanup by default."""
        mock_docker, mock_client = create_mock_docker()
        mock_client.containers.list.return_value = []
        mock_client.ping.return_value = True

        with patch.dict(sys.modules, {"docker": mock_docker}):
            out, _ = self.call_command()

            self.assertIn("No orphaned containers found", out)

    def test_command_runs_all_cleanup(self):
        """Test that command removes all containers with --all flag."""
        mock_docker, mock_client = create_mock_docker()
        mock_client.containers.list.return_value = []
        mock_client.ping.return_value = True

        with patch.dict(sys.modules, {"docker": mock_docker}):
            out, _ = self.call_command("--all")

            self.assertIn("No containers to remove", out)

    def test_command_dry_run_shows_containers(self):
        """Test that --dry-run shows containers without removing them."""
        mock_docker, mock_client = create_mock_docker()
        mock_client.ping.return_value = True

        # Create mock containers
        containers = [
            create_mock_container("abc123", run_id="run-1"),
            create_mock_container("def456", run_id="run-2"),
        ]
        mock_client.containers.list.return_value = containers

        with patch.dict(sys.modules, {"docker": mock_docker}):
            out, _ = self.call_command("--dry-run")

            self.assertIn("Found 2", out)
            self.assertIn("abc123", out)
            self.assertIn("def456", out)

            # Verify containers were NOT removed
            for container in containers:
                container.remove.assert_not_called()


class ContainerLabelsTests(TestCase):
    """Tests for container labeling in DockerValidatorRunner.run()."""

    def test_run_adds_labels_to_container(self):
        """Test that run() adds Validibot labels to spawned containers."""
        mock_docker, mock_client = create_mock_docker()

        mock_container = MagicMock()
        mock_container.short_id = "test123"
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.logs.return_value = b"container output"
        mock_client.containers.run.return_value = mock_container

        with patch.dict(sys.modules, {"docker": mock_docker}):
            from validibot.validations.services.runners.docker import LABEL_MANAGED
            from validibot.validations.services.runners.docker import LABEL_RUN_ID
            from validibot.validations.services.runners.docker import LABEL_STARTED_AT
            from validibot.validations.services.runners.docker import (
                LABEL_TIMEOUT_SECONDS,
            )
            from validibot.validations.services.runners.docker import LABEL_VALIDATOR
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )

            runner = DockerValidatorRunner()
            runner._client = mock_client

            runner.run(
                container_image="test-image:latest",
                input_uri="file:///input.json",
                output_uri="file:///output.json",
                run_id="my-run-123",
                validator_slug="energyplus",
            )

            # Check that containers.run was called with labels
            call_kwargs = mock_client.containers.run.call_args[1]
            labels = call_kwargs.get("labels", {})

            self.assertEqual(labels[LABEL_MANAGED], "true")
            self.assertEqual(labels[LABEL_RUN_ID], "my-run-123")
            self.assertEqual(labels[LABEL_VALIDATOR], "energyplus")
            self.assertIn(LABEL_STARTED_AT, labels)
            self.assertIn(LABEL_TIMEOUT_SECONDS, labels)

    def test_run_applies_security_hardening(self):
        """Test that run() applies security hardening options to containers."""
        mock_docker, mock_client = create_mock_docker()

        mock_container = MagicMock()
        mock_container.short_id = "test123"
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.logs.return_value = b""
        mock_client.containers.run.return_value = mock_container

        with patch.dict(sys.modules, {"docker": mock_docker}):
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )

            runner = DockerValidatorRunner()
            runner._client = mock_client

            runner.run(
                container_image="test-image:latest",
                input_uri="file:///input.json",
                output_uri="file:///output.json",
            )

            call_kwargs = mock_client.containers.run.call_args[1]

            # All Linux capabilities should be dropped
            self.assertEqual(call_kwargs["cap_drop"], ["ALL"])
            # Privilege escalation should be blocked
            self.assertEqual(
                call_kwargs["security_opt"],
                ["no-new-privileges:true"],
            )
            # PID limit should be set to prevent fork bombs
            self.assertEqual(call_kwargs["pids_limit"], 512)
            # Root filesystem should be read-only
            self.assertTrue(call_kwargs["read_only"])
            # Writable tmpfs should be mounted for validator scratch space
            self.assertIn("/tmp", call_kwargs["tmpfs"])  # noqa: S108
            # Container should run as non-root user
            self.assertEqual(call_kwargs["user"], "1000:1000")
            # Network should be isolated
            self.assertEqual(call_kwargs["network_mode"], "none")

    def test_run_passes_run_id_as_environment_variable(self):
        """Test that run() passes VALIDIBOT_RUN_ID to the container."""
        mock_docker, mock_client = create_mock_docker()

        mock_container = MagicMock()
        mock_container.short_id = "test123"
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.logs.return_value = b""
        mock_client.containers.run.return_value = mock_container

        with patch.dict(sys.modules, {"docker": mock_docker}):
            from validibot.validations.services.runners.docker import (
                DockerValidatorRunner,
            )

            runner = DockerValidatorRunner()
            runner._client = mock_client

            runner.run(
                container_image="test-image:latest",
                input_uri="file:///input.json",
                output_uri="file:///output.json",
                run_id="my-run-123",
            )

            # Check that environment includes VALIDIBOT_RUN_ID
            call_kwargs = mock_client.containers.run.call_args[1]
            env = call_kwargs.get("environment", {})

            self.assertEqual(env["VALIDIBOT_RUN_ID"], "my-run-123")
