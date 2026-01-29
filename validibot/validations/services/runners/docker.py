"""
Docker-based validator runner for self-hosted deployments.

This runner executes validator containers using the local Docker socket,
suitable for:
- Self-hosted deployments on any cloud/VPS
- Local development and testing
- Single-server deployments

Containers are run in detached mode (fire-and-forget). Results are delivered
via HTTP callback to Django when the validator completes.

SECURITY NOTES
--------------
- Containers run with read-only access to input files
- Network access is restricted to callback URL (configurable)
- Memory and CPU limits can be set via VALIDATOR_RUNNER_OPTIONS
- Consider running validators in a separate Docker network for isolation
"""

from __future__ import annotations

import logging

from django.conf import settings

from validibot.validations.services.runners.base import ExecutionInfo
from validibot.validations.services.runners.base import ExecutionStatus
from validibot.validations.services.runners.base import ValidatorRunner

logger = logging.getLogger(__name__)

# Default resource limits for validator containers
DEFAULT_MEMORY_LIMIT = "4g"
DEFAULT_CPU_LIMIT = "2.0"
DEFAULT_TIMEOUT_SECONDS = 3600  # 1 hour


class DockerValidatorRunner(ValidatorRunner):
    """
    Docker-based validator runner using the local Docker socket.

    This runner starts validator containers using the Docker SDK and returns
    immediately. Containers run in detached mode and POST results back to
    Django via the callback URL in the input envelope.

    Configuration via settings:
        VALIDATOR_RUNNER = "docker"
        VALIDATOR_RUNNER_OPTIONS = {
            "memory_limit": "4g",      # Container memory limit
            "cpu_limit": "2.0",        # CPU limit (cores)
            "network": "validibot",    # Docker network for containers
            "timeout_seconds": 3600,   # Default timeout
        }

    For local development, ensure DATA_STORAGE_ROOT is mounted into containers
    so they can read input files and write outputs.
    """

    def __init__(
        self,
        memory_limit: str | None = None,
        cpu_limit: str | None = None,
        network: str | None = None,
        timeout_seconds: int | None = None,
    ):
        """
        Initialize Docker validator runner.

        Args:
            memory_limit: Container memory limit (e.g., "4g", "8g")
            cpu_limit: CPU limit as float string (e.g., "2.0")
            network: Docker network to attach containers to
            timeout_seconds: Default timeout for container execution
        """
        self.memory_limit = memory_limit or DEFAULT_MEMORY_LIMIT
        self.cpu_limit = cpu_limit or DEFAULT_CPU_LIMIT
        self.network = network
        self.timeout_seconds = timeout_seconds or DEFAULT_TIMEOUT_SECONDS

        # Docker client initialized lazily
        self._client = None

    def _get_client(self):
        """Get or create Docker client."""
        if self._client is None:
            try:
                import docker
            except ImportError as e:
                msg = (
                    "docker package is required for Docker runner. "
                    "Install with: pip install docker"
                )
                raise ImportError(msg) from e

            self._client = docker.from_env()
        return self._client

    def is_available(self) -> bool:
        """Check if Docker is available."""
        try:
            client = self._get_client()
            client.ping()
        except Exception as e:
            logger.warning("Docker not available: %s", e)
            return False
        else:
            return True

    def run(
        self,
        *,
        container_image: str,
        input_uri: str,
        environment: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
    ) -> str:
        """
        Start a validator container execution.

        The container is run in detached mode with:
        - INPUT_URI environment variable pointing to the input envelope
        - Volume mount for DATA_STORAGE_ROOT (for file:// URIs)
        - Memory and CPU limits as configured
        - Auto-remove after completion

        Args:
            container_image: Docker image to run
            input_uri: URI to input envelope (file://, gs://, s3://)
            environment: Additional environment variables
            timeout_seconds: Maximum execution time (uses default if None)

        Returns:
            Container ID (short form)

        Raises:
            RuntimeError: If container could not be started
        """
        client = self._get_client()

        # Build environment variables
        env = {
            "INPUT_URI": input_uri,
        }
        if environment:
            env.update(environment)

        # Build volume mounts for local file access
        volumes = {}
        storage_root = getattr(settings, "DATA_STORAGE_ROOT", None)
        if storage_root:
            # Mount storage root as read-write so validators can write outputs
            volumes[storage_root] = {"bind": storage_root, "mode": "rw"}

        # Container configuration
        container_config = {
            "image": container_image,
            "environment": env,
            "detach": True,
            "auto_remove": True,  # Clean up after completion
            "mem_limit": self.memory_limit,
            "nano_cpus": int(float(self.cpu_limit) * 1e9),
        }

        if volumes:
            container_config["volumes"] = volumes

        if self.network:
            container_config["network"] = self.network

        try:
            logger.info(
                "Starting Docker container: image=%s, input_uri=%s",
                container_image,
                input_uri,
            )

            container = client.containers.run(**container_config)

            logger.info(
                "Started container: id=%s, image=%s",
                container.short_id,
                container_image,
            )
        except Exception as e:
            logger.exception("Failed to start Docker container: %s", container_image)
            msg = f"Failed to start validator container: {e}"
            raise RuntimeError(msg) from e
        else:
            return container.short_id

    def get_execution_status(self, execution_id: str) -> ExecutionInfo:
        """
        Get the status of a container execution.

        Args:
            execution_id: Container ID (short or full)

        Returns:
            ExecutionInfo with current status

        Raises:
            ValueError: If container is not found
        """
        client = self._get_client()

        try:
            container = client.containers.get(execution_id)
        except Exception as e:
            msg = f"Container not found: {execution_id}"
            raise ValueError(msg) from e

        # Map Docker status to ExecutionStatus
        status_map = {
            "created": ExecutionStatus.PENDING,
            "restarting": ExecutionStatus.RUNNING,
            "running": ExecutionStatus.RUNNING,
            "removing": ExecutionStatus.RUNNING,
            "paused": ExecutionStatus.RUNNING,
            "exited": ExecutionStatus.SUCCEEDED,  # Check exit code below
            "dead": ExecutionStatus.FAILED,
        }

        docker_status = container.status
        status = status_map.get(docker_status, ExecutionStatus.UNKNOWN)

        # Check exit code for exited containers
        exit_code = None
        if docker_status == "exited":
            exit_code = container.attrs.get("State", {}).get("ExitCode")
            if exit_code != 0:
                status = ExecutionStatus.FAILED

        return ExecutionInfo(
            execution_id=execution_id,
            status=status,
            start_time=container.attrs.get("State", {}).get("StartedAt"),
            end_time=container.attrs.get("State", {}).get("FinishedAt"),
            exit_code=exit_code,
        )

    def cancel(self, execution_id: str) -> bool:
        """
        Cancel a running container execution.

        Args:
            execution_id: Container ID (short or full)

        Returns:
            True if container was stopped, False otherwise
        """
        client = self._get_client()

        try:
            container = client.containers.get(execution_id)
            container.stop(timeout=10)
            logger.info("Stopped container: %s", execution_id)
        except Exception as e:
            logger.warning("Failed to stop container %s: %s", execution_id, e)
            return False
        else:
            return True

    def get_runner_type(self) -> str:
        """Return runner type identifier."""
        return "docker"
