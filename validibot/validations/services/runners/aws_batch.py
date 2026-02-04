"""
AWS Batch validator runner.

This runner executes validator containers as AWS Batch jobs, suitable for:
- Production AWS deployments
- Scalable, managed container execution
- Integration with AWS IAM and security model

AWS Batch jobs run asynchronously. Results are delivered via HTTP callback
to Django when the job completes.

STATUS: STUB - Not yet implemented. Install boto3 and complete implementation.
"""

from __future__ import annotations

import logging

from validibot.validations.services.runners.base import ExecutionInfo
from validibot.validations.services.runners.base import ExecutionResult
from validibot.validations.services.runners.base import ValidatorRunner

logger = logging.getLogger(__name__)


class AWSBatchValidatorRunner(ValidatorRunner):
    """
    AWS Batch validator runner.

    This runner starts AWS Batch jobs for container validation. Jobs run
    asynchronously and POST results back to Django via callback.

    Configuration via settings:
        VALIDATOR_RUNNER = "aws_batch"
        VALIDATOR_RUNNER_OPTIONS = {
            "job_queue": "validibot-validators",
            "region": "ap-southeast-2",
        }

    STATUS: STUB - Not yet implemented.

    Implementation notes for future work:
    - Use boto3.client("batch") for job submission
    - Job definitions should be pre-created in AWS Batch
    - Map container images to job definition names (similar to Cloud Run Jobs)
    - Pass VALIDIBOT_INPUT_URI as environment variable override
    - Use S3 for input/output files
    """

    def __init__(
        self,
        job_queue: str | None = None,
        region: str | None = None,
    ):
        """
        Initialize AWS Batch validator runner.

        Args:
            job_queue: AWS Batch job queue name
            region: AWS region
        """
        self.job_queue = job_queue
        self.region = region

        # Client initialized lazily
        self._client = None

    def _get_client(self):
        """Get or create boto3 Batch client."""
        if self._client is None:
            try:
                import boto3
            except ImportError as e:
                msg = (
                    "boto3 is required for AWS Batch runner. "
                    "Install with: pip install boto3"
                )
                raise ImportError(msg) from e

            self._client = boto3.client("batch", region_name=self.region)
        return self._client

    def is_available(self) -> bool:
        """Check if AWS Batch API is available."""
        try:
            self._get_client()
        except Exception as e:
            logger.warning("AWS Batch API not available: %s", e)
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
    ) -> ExecutionResult:
        """
        Run an AWS Batch job and wait for completion.

        STATUS: STUB - Not yet implemented.

        Args:
            container_image: Container image (used to derive job definition name)
            input_uri: URI to input envelope (should be s3:// for AWS)
            output_uri: URI where container should write output envelope
            environment: Additional environment variables
            timeout_seconds: Job timeout in seconds

        Returns:
            ExecutionResult with exit_code and output_uri

        Raises:
            NotImplementedError: This method is not yet implemented
        """
        raise NotImplementedError(
            "AWSBatchValidatorRunner.run() is not yet implemented. "
            "This is a stub for future AWS support."
        )

    def get_execution_status(self, execution_id: str) -> ExecutionInfo:
        """
        Get the status of an AWS Batch job.

        STATUS: STUB - Not yet implemented.

        Args:
            execution_id: Job ID from run()

        Returns:
            ExecutionInfo with current status

        Raises:
            NotImplementedError: This method is not yet implemented
        """
        raise NotImplementedError(
            "AWSBatchValidatorRunner.get_execution_status() is not yet implemented. "
            "This is a stub for future AWS support."
        )

    def cancel(self, execution_id: str) -> bool:
        """
        Cancel an AWS Batch job.

        STATUS: STUB - Not yet implemented.

        Args:
            execution_id: Job ID from run()

        Returns:
            True if cancellation was successful, False otherwise

        Raises:
            NotImplementedError: This method is not yet implemented
        """
        raise NotImplementedError(
            "AWSBatchValidatorRunner.cancel() is not yet implemented. "
            "This is a stub for future AWS support."
        )

    def get_runner_type(self) -> str:
        """Return runner type identifier."""
        return "aws_batch"
