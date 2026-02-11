"""
Google Cloud Run Jobs validator runner.

This runner executes validator containers as Cloud Run Jobs on Google Cloud,
suitable for:
- Production GCP deployments
- Scalable, serverless validation execution
- Integration with GCP's IAM and security model

Cloud Run Jobs run asynchronously. Results are delivered via HTTP callback
to Django's worker service when the job completes.

ARCHITECTURE
------------
1. Django (web) receives validation request
2. Runner triggers Cloud Run Job with input_uri as environment variable
3. Cloud Run Job reads input envelope from GCS
4. Cloud Run Job writes results to GCS and POSTs callback
5. Django (worker) receives callback and updates validation run

SECURITY
--------
- Jobs run with the Cloud Run service account
- Input/output files are in GCS (private/ prefix with signed URLs)
- Callbacks are authenticated via Cloud Run's IAM (service-to-service auth)
"""

from __future__ import annotations

import logging

from django.conf import settings

from validibot.validations.services.runners.base import ExecutionInfo
from validibot.validations.services.runners.base import ExecutionResult
from validibot.validations.services.runners.base import ExecutionStatus
from validibot.validations.services.runners.base import ValidatorRunner

logger = logging.getLogger(__name__)


class GoogleCloudRunValidatorRunner(ValidatorRunner):
    """
    Google Cloud Run Jobs validator runner.

    This runner starts Cloud Run Jobs using the Jobs API. Jobs run asynchronously
    and POST their results back to Django via callback.

    Configuration via settings:
        VALIDATOR_RUNNER = "google_cloud_run"
        VALIDATOR_RUNNER_OPTIONS = {
            "project_id": "my-gcp-project",
            "region": "us-west1",
        }

    The runner maps container images to pre-deployed Cloud Run Jobs:
        validibot/validator-energyplus -> validibot-validator-energyplus job
        validibot/validator-fmi -> validibot-validator-fmi job

    Jobs must be pre-deployed to Cloud Run with the correct container image.
    The runner triggers executions of existing jobs rather than creating new ones.
    """

    def __init__(
        self,
        project_id: str | None = None,
        region: str | None = None,
    ):
        """
        Initialize Google Cloud Run validator runner.

        Args:
            project_id: GCP project ID. If None, uses settings.GCP_PROJECT_ID
            region: GCP region. If None, uses settings.GCP_REGION
        """
        self.project_id = project_id or getattr(settings, "GCP_PROJECT_ID", "")
        self.region = region or getattr(settings, "GCP_REGION", "")

        if not self.project_id:
            msg = "GCP_PROJECT_ID is required for Google Cloud Run runner"
            raise ValueError(msg)
        if not self.region:
            msg = "GCP_REGION is required for Google Cloud Run runner"
            raise ValueError(msg)

        # Client initialized lazily
        self._jobs_client = None
        self._executions_client = None

    def _get_jobs_client(self):
        """Get or create Cloud Run Jobs client."""
        if self._jobs_client is None:
            try:
                from google.cloud import run_v2
            except ImportError as e:
                msg = (
                    "google-cloud-run is required for Cloud Run runner. "
                    "Install with: pip install google-cloud-run"
                )
                raise ImportError(msg) from e

            self._jobs_client = run_v2.JobsClient()
        return self._jobs_client

    def _get_executions_client(self):
        """Get or create Cloud Run Executions client."""
        if self._executions_client is None:
            from google.cloud import run_v2

            self._executions_client = run_v2.ExecutionsClient()
        return self._executions_client

    def is_available(self) -> bool:
        """Check if Cloud Run API is available."""
        try:
            # Try to import and create client
            self._get_jobs_client()
        except Exception as e:
            logger.warning("Cloud Run API not available: %s", e)
            return False
        else:
            return True

    def _image_to_job_name(self, container_image: str) -> str:
        """
        Map a container image to the corresponding Cloud Run Job name.

        Convention: image name maps to job name with the same identifier.
        Example: "validibot/validator-energyplus:latest"
            -> "validibot-validator-energyplus"

        Args:
            container_image: Container image name

        Returns:
            Cloud Run Job name
        """
        # Extract image name without registry and tag
        # e.g., "gcr.io/project/validibot-validator-energyplus:v1"
        # -> "validibot-validator-energyplus"
        # Get last part after /
        image_parts = container_image.rsplit("/", maxsplit=1)[-1]
        job_name = image_parts.split(":")[0]  # Remove tag
        return job_name

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
        Run a Cloud Run Job synchronously.

        Cloud Run Jobs are inherently asynchronous, so this method is not
        supported. Use run_async() instead and handle results via callbacks.

        Raises:
            NotImplementedError: Always. Use run_async() instead.
        """
        raise NotImplementedError(
            "GoogleCloudRunValidatorRunner does not support synchronous execution. "
            "Cloud Run Jobs are async - use run_async() and handle results via "
            "callback when the job completes."
        )

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
        Start a Cloud Run Job execution asynchronously.

        The job must already be deployed to Cloud Run. This method triggers
        an execution of the existing job with the given input/output URIs.
        Results are delivered via callback when the job completes.

        Args:
            container_image: Container image (used to derive job name)
            input_uri: URI to input envelope (must be gs:// for Cloud Run)
            output_uri: URI where container should write output envelope
            environment: Additional environment variables
            timeout_seconds: Not used (job timeout is set in job definition)

        Returns:
            Execution name (projects/.../jobs/.../executions/...)

        Raises:
            RuntimeError: If job execution could not be started
        """
        from google.cloud import run_v2

        client = self._get_jobs_client()
        job_name = self._image_to_job_name(container_image)

        # Build full job path
        job_path = f"projects/{self.project_id}/locations/{self.region}/jobs/{job_name}"

        # Build environment overrides using standardized env var names
        env_vars = [
            run_v2.EnvVar(name="VALIDIBOT_INPUT_URI", value=input_uri),
            run_v2.EnvVar(name="VALIDIBOT_OUTPUT_URI", value=output_uri),
        ]
        if environment:
            for key, value in environment.items():
                env_vars.append(run_v2.EnvVar(name=key, value=value))

        request = run_v2.RunJobRequest(
            name=job_path,
            overrides=run_v2.RunJobRequest.Overrides(
                container_overrides=[
                    run_v2.RunJobRequest.Overrides.ContainerOverride(
                        env=env_vars,
                    ),
                ],
            ),
        )

        try:
            logger.info(
                "Starting Cloud Run Job: job=%s, input_uri=%s, output_uri=%s",
                job_name,
                input_uri,
                output_uri,
            )

            # run_job returns a long-running operation
            operation = client.run_job(request=request)

            # Extract execution name from operation metadata without blocking
            execution_name = getattr(operation.metadata, "name", None)
            if not execution_name:
                # Fall back to waiting briefly for the operation
                try:
                    execution = operation.result(timeout=30)
                    execution_name = execution.name
                except Exception as exc:
                    logger.exception(
                        "Cloud Run Job started but execution name unavailable"
                    )
                    msg = "Cloud Run Job started but execution name not available"
                    raise RuntimeError(msg) from exc

            logger.info("Started Cloud Run execution: %s", execution_name)
        except Exception as e:
            logger.exception("Failed to start Cloud Run Job: %s", job_name)
            msg = f"Failed to start Cloud Run Job: {e}"
            raise RuntimeError(msg) from e
        else:
            return execution_name

    def get_execution_status(self, execution_id: str) -> ExecutionInfo:
        """
        Get the status of a Cloud Run Job execution.

        Args:
            execution_id: Full execution name from run()

        Returns:
            ExecutionInfo with current status

        Raises:
            ValueError: If execution is not found
        """
        from google.cloud import run_v2

        client = self._get_executions_client()

        try:
            execution = client.get_execution(name=execution_id)
        except Exception as e:
            msg = f"Execution not found: {execution_id}"
            raise ValueError(msg) from e

        # Map Cloud Run condition to ExecutionStatus
        status = ExecutionStatus.UNKNOWN
        for condition in execution.conditions:
            if condition.type_ == "Completed":
                if condition.state == run_v2.Condition.State.CONDITION_SUCCEEDED:
                    status = ExecutionStatus.SUCCEEDED
                elif condition.state == run_v2.Condition.State.CONDITION_FAILED:
                    status = ExecutionStatus.FAILED
                break

        # If no completion condition, infer running/pending
        if status == ExecutionStatus.UNKNOWN:
            if execution.start_time and not execution.completion_time:
                status = ExecutionStatus.RUNNING
            else:
                status = ExecutionStatus.PENDING

        return ExecutionInfo(
            execution_id=execution_id,
            status=status,
            start_time=str(execution.start_time) if execution.start_time else None,
            end_time=(
                str(execution.completion_time) if execution.completion_time else None
            ),
        )

    def cancel(self, execution_id: str) -> bool:
        """
        Cancel a Cloud Run Job execution.

        Cloud Run Jobs support cancellation via the delete operation on
        the execution.

        Args:
            execution_id: Full execution name from run()

        Returns:
            True if cancellation was successful, False otherwise
        """
        client = self._get_executions_client()

        try:
            client.delete_execution(name=execution_id)
            logger.info("Cancelled Cloud Run execution: %s", execution_id)
        except Exception as e:
            logger.warning("Failed to cancel execution %s: %s", execution_id, e)
            return False
        else:
            return True

    def get_runner_type(self) -> str:
        """Return runner type identifier."""
        return "google_cloud_run"
