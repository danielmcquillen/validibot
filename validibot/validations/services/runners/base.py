"""
Abstract base class for validator runners.

Validator runners execute container-based validators (advanced validators like
EnergyPlus, FMI, custom containers). Each runner implementation targets a
specific execution environment:

- DockerValidatorRunner: Local Docker socket (self-hosted deployments)
- GoogleCloudRunValidatorRunner: Google Cloud Run Jobs (GCP deployments)
- AWSBatchValidatorRunner: AWS Batch (AWS deployments)

## Execution Model

The runner interface supports **synchronous execution**:
1. Caller invokes run() which blocks until the container completes
2. Container reads input envelope from INPUT_URI environment variable
3. Container writes output envelope to OUTPUT_URI (or convention-based path)
4. Container exits with status code (0 = success)
5. run() returns ExecutionResult with exit_code and output_uri

This design allows the Dramatiq worker to wait for container completion and
then process the output envelope directly.

For Google Cloud Run Jobs, which are inherently async, the runner provides
run_async() which returns immediately with an execution_id, and a separate
callback mechanism handles result delivery.
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


@dataclass
class ExecutionResult:
    """
    Result of a synchronous container execution.

    Returned by run() after the container completes (or times out).
    Contains enough information to locate and process the output envelope.
    """

    execution_id: str
    exit_code: int
    output_uri: str | None = None
    logs: str | None = None
    error_message: str | None = None
    duration_seconds: float | None = None

    @property
    def succeeded(self) -> bool:
        """Return True if the container exited successfully (exit code 0)."""
        return self.exit_code == 0


class ValidatorRunner(ABC):
    """
    Abstract base class for validator container runners.

    Implementations handle execution of container-based validators on their
    respective platforms (Docker, Google Cloud Run, AWS Batch).

    ## Synchronous Execution (Primary Mode)

    The run() method blocks until the container completes:
    1. run() starts the container and waits for it to exit
    2. The container reads its input envelope from VALIDIBOT_INPUT_URI
    3. The container writes output envelope to VALIDIBOT_OUTPUT_URI
    4. Container exits with status code (0 = success)
    5. run() returns ExecutionResult with exit_code and output_uri

    This is the preferred mode for self-hosted deployments where the Dramatiq
    worker can wait for completion.

    ## Async Execution (for Cloud Run Jobs)

    For platforms that don't support blocking execution (Cloud Run Jobs),
    use run_async() which returns immediately with an execution_id. Results
    are delivered via callback when the job completes.
    """

    @abstractmethod
    def run(
        self,
        *,
        container_image: str,
        input_uri: str,
        output_uri: str,
        environment: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
    ) -> ExecutionResult:
        """
        Run a validator container and wait for completion.

        This method blocks until the container exits or times out.

        Args:
            container_image: Container image to run
                (e.g., "validibot/validator-energyplus:latest")
            input_uri: URI to input envelope JSON (file://, gs://, or s3://)
            output_uri: URI where container should write output envelope
            environment: Additional environment variables to pass to container
            timeout_seconds: Maximum execution time
                (implementation-specific default if None)

        Returns:
            ExecutionResult with exit_code, output_uri, and optional logs

        Raises:
            RuntimeError: If container execution could not be started
            TimeoutError: If container did not complete within timeout
        """

    def run_async(
        self,
        *,
        container_image: str,
        input_uri: str,
        output_uri: str,
        environment: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
    ) -> str:
        """
        Start a validator container without waiting for completion.

        This method starts the container and returns immediately. Use this
        for runners that don't support synchronous execution (like Cloud Run
        Jobs) or when you want fire-and-forget execution with callbacks.

        Args:
            container_image: Container image to run
            input_uri: URI to input envelope JSON
            output_uri: URI where container should write output envelope
            environment: Additional environment variables
            timeout_seconds: Maximum execution time

        Returns:
            Execution identifier for tracking (Docker container ID,
            Cloud Run execution name, AWS Batch job ID)

        Raises:
            RuntimeError: If container execution could not be started
            NotImplementedError: If runner doesn't support async execution
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support async execution. "
            "Use run() for synchronous execution."
        )

    def get_execution_status(self, execution_id: str) -> ExecutionInfo:
        """
        Get the status of an async container execution.

        Only applicable for async executions started with run_async().
        For synchronous executions, the result is returned by run() directly.

        Args:
            execution_id: Execution identifier returned by run_async()

        Returns:
            ExecutionInfo with current status

        Raises:
            ValueError: If execution_id is not found
            NotImplementedError: If runner doesn't support status checking
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support status checking. "
            "Use run() for synchronous execution."
        )

    def cancel(self, execution_id: str) -> bool:
        """
        Cancel a running execution.

        This attempts to stop a running container. Not all runners may
        support cancellation (in which case they should return False).

        Args:
            execution_id: Execution identifier returned by run() or run_async()

        Returns:
            True if cancellation was successful, False otherwise
        """
        return False

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
