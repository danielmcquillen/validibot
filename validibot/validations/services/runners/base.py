"""
Abstract base class for validator runners.

Validator runners execute container-based validators (advanced validators like
EnergyPlus, FMI, custom containers). Each runner implementation targets a
specific execution environment:

- DockerValidatorRunner: Local Docker socket (self-hosted deployments)
- GoogleCloudRunValidatorRunner: Google Cloud Run Jobs (GCP deployments)
- AWSBatchValidatorRunner: AWS Batch (AWS deployments)

The runner interface is intentionally minimal. Runners are responsible only for:
1. Starting container execution with the given input envelope
2. Returning an execution identifier for tracking
3. Optionally checking execution status

Results are delivered via callbacks (the container POSTs to Django when done),
not by polling the runner. This enables fire-and-forget async execution.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from enum import Enum


class ExecutionStatus(str, Enum):
    """
    Status of a container execution.

    These map to the common states across Docker, Cloud Run Jobs, and AWS Batch.
    """

    PENDING = "PENDING"  # Queued but not yet started
    RUNNING = "RUNNING"  # Container is executing
    SUCCEEDED = "SUCCEEDED"  # Completed successfully
    FAILED = "FAILED"  # Completed with error
    CANCELLED = "CANCELLED"  # Stopped before completion
    UNKNOWN = "UNKNOWN"  # Status could not be determined


@dataclass
class ExecutionInfo:
    """
    Information about a container execution.

    Returned by get_execution_status() to provide details about a running
    or completed execution.
    """

    execution_id: str
    status: ExecutionStatus
    start_time: str | None = None
    end_time: str | None = None
    exit_code: int | None = None
    error_message: str | None = None


class ValidatorRunner(ABC):
    """
    Abstract base class for validator container runners.

    Implementations handle execution of container-based validators on their
    respective platforms (Docker, Google Cloud Run, AWS Batch).

    The execution model is async/callback-based:
    1. run() starts the container and returns immediately with an execution_id
    2. The container reads its input envelope from input_uri
    3. The container writes results and POSTs to the callback URL
    4. Django receives the callback and updates the validation run

    This design allows running long validations (minutes to hours) without
    blocking Django workers or maintaining long-lived connections.
    """

    @abstractmethod
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

        This method starts the container and returns immediately without
        waiting for completion. Results are delivered via callback.

        Args:
            container_image: Container image to run
                (e.g., "validibot/validator-energyplus:latest")
            input_uri: URI to input envelope JSON (file://, gs://, or s3://)
            environment: Additional environment variables to pass to container
            timeout_seconds: Maximum execution time
                (implementation-specific default if None)

        Returns:
            Execution identifier for tracking (format varies by runner:
            Docker container ID, Cloud Run execution name, AWS Batch job ID)

        Raises:
            RuntimeError: If container execution could not be started
        """

    @abstractmethod
    def get_execution_status(self, execution_id: str) -> ExecutionInfo:
        """
        Get the status of a container execution.

        This is primarily for debugging and monitoring. The normal result
        delivery path is via callbacks, not polling.

        Args:
            execution_id: Execution identifier returned by run()

        Returns:
            ExecutionInfo with current status

        Raises:
            ValueError: If execution_id is not found
        """

    @abstractmethod
    def cancel(self, execution_id: str) -> bool:
        """
        Cancel a running execution.

        This attempts to stop a running container. Not all runners may
        support cancellation (in which case they should return False).

        Args:
            execution_id: Execution identifier returned by run()

        Returns:
            True if cancellation was successful, False otherwise
        """

    def is_available(self) -> bool:
        """
        Check if this runner is available for use.

        Implementations should check for required dependencies (Docker socket,
        cloud credentials, etc.) and return False if they're not available.

        Returns:
            True if the runner can execute containers, False otherwise
        """
        return True

    def get_runner_type(self) -> str:
        """
        Get the runner type identifier.

        Returns:
            Short identifier for this runner type
            (e.g., "docker", "google_cloud_run", "aws_batch")
        """
        return self.__class__.__name__.replace("ValidatorRunner", "").lower()
