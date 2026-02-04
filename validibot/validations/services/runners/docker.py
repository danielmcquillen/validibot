"""
Docker-based validator runner for Docker Compose deployments.

This runner executes validator containers using the local Docker socket,
suitable for:
- Docker Compose deployments on any cloud/VPS
- Local development and testing
- Single-server deployments

## Execution Model

Containers run **synchronously** - the run() method blocks until the container
exits or times out. The Celery worker waits for completion and then reads
the output envelope from storage.

Environment variables passed to containers:
- VALIDIBOT_INPUT_URI: Location of input envelope
- VALIDIBOT_OUTPUT_URI: Where to write output envelope
- VALIDIBOT_RUN_ID: Validation run ID (for logging)

## Container Labels (Ryuk Pattern)

All spawned containers are labeled for robust cleanup:
- org.validibot.managed: "true" (identifies Validibot containers)
- org.validibot.run_id: validation run ID
- org.validibot.validator: validator slug
- org.validibot.started_at: ISO timestamp
- org.validibot.timeout_seconds: configured timeout

This enables three cleanup strategies:
1. On-demand cleanup after each run (normal path)
2. Periodic sweep for orphaned containers (via cleanup_orphaned_containers())
3. Startup cleanup for containers from crashed workers

SECURITY NOTES
--------------
- Containers run with read-only access to input files
- Network access is restricted to callback URL (configurable)
- Memory and CPU limits can be set via VALIDATOR_RUNNER_OPTIONS
- Consider running validators in a separate Docker network for isolation
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC
from datetime import datetime

from django.conf import settings

from validibot.validations.services.runners.base import ExecutionInfo
from validibot.validations.services.runners.base import ExecutionResult
from validibot.validations.services.runners.base import ExecutionStatus
from validibot.validations.services.runners.base import ValidatorRunner

logger = logging.getLogger(__name__)

# Default resource limits for validator containers
DEFAULT_MEMORY_LIMIT = "4g"
DEFAULT_CPU_LIMIT = "2.0"
DEFAULT_TIMEOUT_SECONDS = 3600  # 1 hour

# Container label prefix for Validibot-managed containers
LABEL_PREFIX = "org.validibot"
LABEL_MANAGED = f"{LABEL_PREFIX}.managed"
LABEL_RUN_ID = f"{LABEL_PREFIX}.run_id"
LABEL_VALIDATOR = f"{LABEL_PREFIX}.validator"
LABEL_STARTED_AT = f"{LABEL_PREFIX}.started_at"
LABEL_TIMEOUT_SECONDS = f"{LABEL_PREFIX}.timeout_seconds"


class DockerValidatorRunner(ValidatorRunner):
    """
    Docker-based validator runner using the local Docker socket.

    This runner executes validator containers synchronously - run() blocks
    until the container exits or times out. The Celery worker waits for
    completion and then reads the output envelope from storage.

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
        storage_volume: str | None = None,
        storage_mount_path: str | None = None,
    ):
        """
        Initialize Docker validator runner.

        Args:
            memory_limit: Container memory limit (e.g., "4g", "8g")
            cpu_limit: CPU limit as float string (e.g., "2.0")
            network: Docker network to attach containers to
            timeout_seconds: Default timeout for container execution
            storage_volume: Docker volume name for storage
                (e.g., "validibot_local_storage")
            storage_mount_path: Path to mount storage volume (e.g., "/app/storage")
        """
        self.memory_limit = memory_limit or DEFAULT_MEMORY_LIMIT
        self.cpu_limit = cpu_limit or DEFAULT_CPU_LIMIT
        self.network = network
        self.timeout_seconds = timeout_seconds or DEFAULT_TIMEOUT_SECONDS
        self.storage_volume = storage_volume
        self.storage_mount_path = storage_mount_path or "/app/storage"

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
        output_uri: str,
        environment: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        run_id: str | None = None,
        validator_slug: str | None = None,
    ) -> ExecutionResult:
        """
        Run a validator container and wait for completion.

        The container runs synchronously - this method blocks until the
        container exits or times out. Container receives:
        - VALIDIBOT_INPUT_URI: Location of input envelope
        - VALIDIBOT_OUTPUT_URI: Where to write output envelope

        Args:
            container_image: Docker image to run
            input_uri: URI to input envelope (file://, gs://, s3://)
            output_uri: URI where container should write output envelope
            environment: Additional environment variables
            timeout_seconds: Maximum execution time (uses default if None)
            run_id: Validation run ID for labeling (enables orphan cleanup)
            validator_slug: Validator slug for labeling

        Returns:
            ExecutionResult with exit_code, output_uri, and logs

        Raises:
            RuntimeError: If container could not be started
            TimeoutError: If container did not complete within timeout
        """
        import time

        client = self._get_client()
        timeout = timeout_seconds or self.timeout_seconds
        start_time = time.time()

        # Build environment variables per ADR spec
        env = {
            "VALIDIBOT_INPUT_URI": input_uri,
            "VALIDIBOT_OUTPUT_URI": output_uri,
        }
        if run_id:
            env["VALIDIBOT_RUN_ID"] = run_id
        if environment:
            env.update(environment)

        # Build container labels for orphan cleanup (Ryuk pattern)
        labels = {
            LABEL_MANAGED: "true",
            LABEL_STARTED_AT: datetime.now(UTC).isoformat(),
            LABEL_TIMEOUT_SECONDS: str(timeout),
        }
        if run_id:
            labels[LABEL_RUN_ID] = run_id
        if validator_slug:
            labels[LABEL_VALIDATOR] = validator_slug

        # Build volume mounts for storage access
        volumes = {}

        # If a named volume is configured, use it (for Docker-in-Docker scenarios)
        if self.storage_volume:
            volumes[self.storage_volume] = {
                "bind": self.storage_mount_path,
                "mode": "rw",
            }
        else:
            # Fall back to host path mounting (for direct Docker usage)
            storage_root = getattr(settings, "DATA_STORAGE_ROOT", None)
            if storage_root:
                # Mount storage root as read-write so validators can write outputs
                volumes[storage_root] = {"bind": storage_root, "mode": "rw"}

        # Container configuration - NOT detached, we wait for completion
        container_config = {
            "image": container_image,
            "environment": env,
            "labels": labels,
            "detach": True,  # Still detach to get container object
            "mem_limit": self.memory_limit,
            "nano_cpus": int(float(self.cpu_limit) * 1e9),
        }

        if volumes:
            container_config["volumes"] = volumes

        if self.network:
            container_config["network"] = self.network

        container = None
        try:
            logger.info(
                "Starting Docker container: image=%s, input_uri=%s, output_uri=%s",
                container_image,
                input_uri,
                output_uri,
            )

            container = client.containers.run(**container_config)
            container_id = container.short_id

            logger.info(
                "Started container: id=%s, image=%s, waiting for completion...",
                container_id,
                container_image,
            )

            # Wait for container to complete
            result = container.wait(timeout=timeout)
            exit_code = result.get("StatusCode", -1)
            duration = time.time() - start_time

            # Get container logs
            logs = None
            try:
                logs = container.logs(stdout=True, stderr=True).decode("utf-8")
            except Exception as log_err:
                logger.warning("Could not retrieve container logs: %s", log_err)

            # Determine error message if failed
            error_message = None
            if exit_code != 0:
                error_message = (
                    result.get("Error") or f"Container exited with code {exit_code}"
                )
                logger.warning(
                    "Container %s failed: exit_code=%d, error=%s",
                    container_id,
                    exit_code,
                    error_message,
                )
            else:
                logger.info(
                    "Container %s completed successfully in %.1fs",
                    container_id,
                    duration,
                )

            return ExecutionResult(
                execution_id=container_id,
                exit_code=exit_code,
                output_uri=output_uri,
                logs=logs,
                error_message=error_message,
                duration_seconds=duration,
            )

        except Exception as e:
            # Handle timeout specifically
            if "timed out" in str(e).lower() or "timeout" in str(e).lower():
                logger.warning(
                    "Container timed out after %ds: %s", timeout, container_image
                )
                # Try to stop the container on timeout
                if container:
                    with contextlib.suppress(Exception):
                        container.stop(timeout=10)
                msg = f"Validator container timed out after {timeout}s"
                raise TimeoutError(msg) from e

            logger.exception("Failed to run Docker container: %s", container_image)
            msg = f"Failed to run validator container: {e}"
            raise RuntimeError(msg) from e
        finally:
            # Clean up container
            if container:
                try:
                    container.remove(force=True)
                except Exception as cleanup_err:
                    logger.debug("Could not remove container: %s", cleanup_err)

    def get_execution_status(self, execution_id: str) -> ExecutionInfo:
        """
        Get the status of a container execution.

        Note: This is primarily for debugging. The normal flow uses run()
        which blocks until completion and returns the result directly.

        Args:
            execution_id: Container ID (short or full)

        Returns:
            ExecutionInfo with current status

        Raises:
            ValueError: If container is not found (may have been cleaned up)
        """
        client = self._get_client()

        try:
            container = client.containers.get(execution_id)
        except Exception as e:
            msg = f"Container not found: {execution_id} (may have been cleaned up)"
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

    def list_managed_containers(self) -> list:
        """
        List all Validibot-managed containers.

        Returns:
            List of Docker container objects with org.validibot.managed=true label.
        """
        client = self._get_client()
        return client.containers.list(
            all=True,
            filters={"label": f"{LABEL_MANAGED}=true"},
        )

    def cleanup_orphaned_containers(
        self,
        grace_period_seconds: int = 300,
    ) -> tuple[int, int]:
        """
        Clean up orphaned Validibot containers.

        Identifies and removes containers that have exceeded their timeout
        plus a grace period. This handles cases where the worker crashed
        before cleaning up.

        Args:
            grace_period_seconds: Extra time to allow beyond the container's
                configured timeout before considering it orphaned (default 5 min).

        Returns:
            Tuple of (containers_removed, containers_failed).
        """
        containers = self.list_managed_containers()
        now = datetime.now(UTC)
        removed = 0
        failed = 0

        for container in containers:
            try:
                # Get container labels
                labels = container.labels
                started_at_str = labels.get(LABEL_STARTED_AT)
                timeout_str = labels.get(LABEL_TIMEOUT_SECONDS, "3600")

                if not started_at_str:
                    # No start time label, skip (shouldn't happen)
                    logger.warning(
                        "Container %s missing started_at label, skipping",
                        container.short_id,
                    )
                    continue

                # Parse start time
                started_at = datetime.fromisoformat(started_at_str)
                timeout = int(timeout_str)
                max_age = timeout + grace_period_seconds

                # Check if container has exceeded max age
                age_seconds = (now - started_at).total_seconds()
                if age_seconds > max_age:
                    run_id = labels.get(LABEL_RUN_ID, "unknown")
                    validator = labels.get(LABEL_VALIDATOR, "unknown")
                    logger.info(
                        "Removing orphaned container: id=%s, run_id=%s, "
                        "validator=%s, age=%.0fs, max_age=%ds",
                        container.short_id,
                        run_id,
                        validator,
                        age_seconds,
                        max_age,
                    )
                    container.remove(force=True)
                    removed += 1

            except Exception:
                logger.exception(
                    "Failed to cleanup container %s", container.short_id
                )
                failed += 1

        if removed > 0 or failed > 0:
            logger.info(
                "Orphan cleanup complete: removed=%d, failed=%d",
                removed,
                failed,
            )

        return removed, failed

    def cleanup_all_managed_containers(self) -> tuple[int, int]:
        """
        Remove all Validibot-managed containers.

        Use this at startup to clean up containers from previous runs
        that may have been left behind due to crashes.

        Returns:
            Tuple of (containers_removed, containers_failed).
        """
        containers = self.list_managed_containers()
        removed = 0
        failed = 0

        for container in containers:
            try:
                run_id = container.labels.get(LABEL_RUN_ID, "unknown")
                validator = container.labels.get(LABEL_VALIDATOR, "unknown")
                logger.info(
                    "Removing leftover container: id=%s, run_id=%s, validator=%s",
                    container.short_id,
                    run_id,
                    validator,
                )
                container.remove(force=True)
                removed += 1
            except Exception:
                logger.exception(
                    "Failed to remove container %s", container.short_id
                )
                failed += 1

        if removed > 0 or failed > 0:
            logger.info(
                "Startup cleanup complete: removed=%d, failed=%d",
                removed,
                failed,
            )

        return removed, failed


def cleanup_orphaned_containers(grace_period_seconds: int = 300) -> tuple[int, int]:
    """
    Module-level function to clean up orphaned Validibot containers.

    This is a convenience function that creates a runner instance and
    calls its cleanup method. Suitable for use in management commands
    or Celery tasks.

    Args:
        grace_period_seconds: Extra time beyond timeout before cleanup.

    Returns:
        Tuple of (containers_removed, containers_failed).
    """
    runner = DockerValidatorRunner()
    if not runner.is_available():
        logger.warning("Docker not available, skipping orphan cleanup")
        return 0, 0
    return runner.cleanup_orphaned_containers(grace_period_seconds)


def cleanup_all_managed_containers() -> tuple[int, int]:
    """
    Module-level function to remove all Validibot-managed containers.

    This is a convenience function for startup cleanup. Use in
    worker AppConfig.ready() or as a management command.

    Returns:
        Tuple of (containers_removed, containers_failed).
    """
    runner = DockerValidatorRunner()
    if not runner.is_available():
        logger.warning("Docker not available, skipping startup cleanup")
        return 0, 0
    return runner.cleanup_all_managed_containers()
