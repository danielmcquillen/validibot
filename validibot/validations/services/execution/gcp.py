"""
GCP execution backend using Cloud Run Jobs.

This backend runs validator containers as Cloud Run Jobs on Google Cloud Platform.
Execution is asynchronous - the job is triggered and returns immediately, with
results delivered later via HTTP callback.

## Execution Flow

```
1. Upload input envelope to GCS (gs://)
2. Trigger Cloud Run Job via Jobs API
3. Return immediately with pending status
4. (Later) Job POSTs results to callback endpoint
5. Callback handler processes results and resumes workflow
```

## When to Use

Use this backend for:
- Production GCP deployments
- High-availability setups with multiple workers
- Deployments requiring IAM-based authentication

## Configuration

Settings:
- `VALIDATOR_RUNNER = "google_cloud_run"`
- `GCP_PROJECT_ID`, `GCP_REGION`
- `GCS_VALIDATION_BUCKET` for file storage
- `WORKER_URL` for callback routing
"""

from __future__ import annotations

import logging

from django.conf import settings

from validibot.validations.services.execution.base import ExecutionBackend
from validibot.validations.services.execution.base import ExecutionRequest
from validibot.validations.services.execution.base import ExecutionResponse

logger = logging.getLogger(__name__)


class GCPExecutionBackend(ExecutionBackend):
    """
    GCP execution backend using Cloud Run Jobs.

    This backend wraps the existing Cloud Run launcher code and provides
    asynchronous execution of validator containers. Results are delivered
    via HTTP callback to the worker service.

    ## Callback Flow

    After triggering a Cloud Run Job, this backend returns a pending response.
    The job container:
    1. Downloads input envelope from GCS
    2. Runs validation
    3. Uploads output envelope to GCS
    4. POSTs callback with result_uri to Django

    The callback is handled by `ValidationCallbackService` which resumes
    the workflow execution.
    """

    def __init__(self) -> None:
        """Initialize the GCP backend."""
        self._project_id = None
        self._region = None

    @property
    def is_async(self) -> bool:
        """GCP execution is asynchronous with callbacks."""
        return True

    @property
    def project_id(self) -> str:
        """GCP project ID."""
        if self._project_id is None:
            self._project_id = getattr(settings, "GCP_PROJECT_ID", "")
        return self._project_id

    @property
    def region(self) -> str:
        """GCP region for Cloud Run Jobs."""
        if self._region is None:
            self._region = getattr(settings, "GCP_REGION", "us-central1")
        return self._region

    def is_available(self) -> bool:
        """Check if GCP Cloud Run is configured."""
        return bool(self.project_id)

    def check_status(self, execution_id: str) -> ExecutionResponse | None:
        """
        Check the status of a Cloud Run Job execution.

        Used by reconciliation to determine if a Cloud Run Job has completed
        but its callback was lost. Delegates to the Cloud Run runner's
        get_execution_status() method.

        Args:
            execution_id: Full Cloud Run execution name
                (projects/.../jobs/.../executions/...).

        Returns:
            ExecutionResponse if the status could be determined, None if the
            Cloud Run SDK is not available or the API call fails.
        """
        try:
            from validibot.validations.services.runners.base import ExecutionStatus
            from validibot.validations.services.runners.google_cloud_run import (
                GoogleCloudRunValidatorRunner,
            )
        except ImportError:
            logger.debug(
                "google-cloud-run not available, cannot check execution status"
            )
            return None

        try:
            runner = GoogleCloudRunValidatorRunner(
                project_id=self.project_id,
                region=self.region,
            )
            info = runner.get_execution_status(execution_id)
        except Exception:
            logger.warning(
                "Failed to check Cloud Run execution status for %s",
                execution_id,
                exc_info=True,
            )
            return None

        return ExecutionResponse(
            execution_id=info.execution_id,
            is_complete=info.status
            in (
                ExecutionStatus.SUCCEEDED,
                ExecutionStatus.FAILED,
            ),
            error_message=info.error_message,
        )

    def get_container_image(self, validator_type: str) -> str:
        """
        Get the container image for a validator type.

        For GCP, images are stored in Artifact Registry.
        """
        vtype = validator_type.lower()

        # Check for explicit job name mapping
        job_names = {
            "energyplus": getattr(settings, "GCS_ENERGYPLUS_JOB_NAME", None),
            "fmu": getattr(settings, "GCS_FMU_JOB_NAME", None),
        }

        if job_names.get(vtype):
            return job_names[vtype]

        # Default naming convention
        return f"validibot-validator-{vtype}"

    def execute(self, request: ExecutionRequest) -> ExecutionResponse:
        """
        Execute a validation via Cloud Run Jobs (async).

        This method delegates to the existing Cloud Run launcher code,
        which handles GCS uploads and job triggering.

        Args:
            request: Execution request with run, validator, submission, step.

        Returns:
            ExecutionResponse with is_complete=False (pending).
        """
        if not self.is_available():
            return ExecutionResponse(
                execution_id="",
                is_complete=True,
                error_message=(
                    "GCP Cloud Run is not configured (GCP_PROJECT_ID not set)"
                ),
            )

        # Delegate to existing launcher based on validator type
        validator_type = request.validator_type.upper()

        try:
            if validator_type == "ENERGYPLUS":
                return self._execute_energyplus(request)
            if validator_type == "FMU":
                return self._execute_fmu(request)
            return ExecutionResponse(
                execution_id="",
                is_complete=True,
                error_message=f"Unsupported validator type for GCP: {validator_type}",
            )

        except Exception as e:
            logger.exception(
                "Failed to launch Cloud Run Job for run %s",
                request.run_id,
            )
            return ExecutionResponse(
                execution_id="",
                is_complete=True,
                error_message=f"Failed to launch Cloud Run Job: {e}",
            )

    def _execute_energyplus(self, request: ExecutionRequest) -> ExecutionResponse:
        """
        Execute EnergyPlus validation via Cloud Run.

        Delegates to the existing launcher function.
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_energyplus_validation,
        )

        # Get ruleset if available
        ruleset = None
        step_config = request.step.config or {}
        ruleset_id = step_config.get("ruleset_id")
        if ruleset_id:
            from validibot.validations.models import Ruleset

            ruleset = Ruleset.objects.filter(id=ruleset_id).first()

        # Launch via existing code
        result = launch_energyplus_validation(
            run=request.run,
            validator=request.validator,
            submission=request.submission,
            ruleset=ruleset,
            step=request.step,
        )

        # Convert ValidationResult to ExecutionResponse
        stats = result.stats or {}
        return ExecutionResponse(
            execution_id=stats.get("execution_name", ""),
            is_complete=False,  # Async - waiting for callback
            input_uri=stats.get("input_uri"),
            output_uri=stats.get("result_uri"),
            execution_bundle_uri=stats.get("execution_bundle_uri"),
        )

    def _execute_fmu(self, request: ExecutionRequest) -> ExecutionResponse:
        """
        Execute FMU validation via Cloud Run.

        Delegates to the existing launcher function.
        """
        from validibot.validations.services.cloud_run.launcher import (
            launch_fmu_validation,
        )

        # Get ruleset if available
        ruleset = None
        step_config = request.step.config or {}
        ruleset_id = step_config.get("ruleset_id")
        if ruleset_id:
            from validibot.validations.models import Ruleset

            ruleset = Ruleset.objects.filter(id=ruleset_id).first()

        # Launch via existing code
        result = launch_fmu_validation(
            run=request.run,
            validator=request.validator,
            submission=request.submission,
            ruleset=ruleset,
            step=request.step,
        )

        # Convert ValidationResult to ExecutionResponse
        stats = result.stats or {}
        return ExecutionResponse(
            execution_id=stats.get("execution_name", ""),
            is_complete=False,  # Async - waiting for callback
            input_uri=stats.get("input_uri"),
            output_uri=stats.get("result_uri"),
            execution_bundle_uri=stats.get("execution_bundle_uri"),
        )
