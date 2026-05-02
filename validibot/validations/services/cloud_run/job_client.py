"""
Cloud Run Job client for triggering validator jobs.

This module triggers Cloud Run Jobs directly using the Jobs API client.
The worker Django service calls this to start heavy validators (EnergyPlus, FMU).

Architecture:
    Web -> Cloud Run Job (this module) -> Callback to worker

The Cloud Run job trigger is intentionally non-blocking. We start the job via
the Jobs API and return immediately; the job runs asynchronously and POSTs its
results back to the worker service.

See issue #64 for context on why we use direct API calls (instead of queueing)
between Django and Cloud Run Jobs.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from google.cloud import run_v2

from validibot.validations.constants import CloudRunJobStatus

if TYPE_CHECKING:
    from google.api_core.operation import Operation

logger = logging.getLogger(__name__)


def run_validator_job(
    *,
    project_id: str,
    region: str,
    job_name: str,
    input_uri: str,
) -> str:
    """
    Start a Cloud Run Job for validation (non-blocking).

    This function triggers a Cloud Run Job directly using the Jobs API and
    returns immediately without waiting for the job to complete. The job
    runs asynchronously and results are delivered via callback.

    The worker's service account (which has roles/run.invoker) provides
    authentication automatically via GCP's metadata service.

    Args:
        project_id: GCP project ID
        region: GCP region (e.g., 'us-west1')
        job_name: Cloud Run Job short name (e.g.,
            'validibot-validator-backend-energyplus').
            Must NOT be fully-qualified (no 'projects/' prefix).
        input_uri: GCS URI to input.json (e.g., 'gs://bucket/runs/abc/input.json')

    Returns:
        Execution name (e.g., 'projects/.../jobs/.../executions/...')
        Can be used for status checks and debugging.

    Raises:
        ValueError: If job_name is fully-qualified (contains 'projects/')
        google.api_core.exceptions.GoogleAPICallError: If job trigger fails

    Example:
        >>> execution_name = run_validator_job(
        ...     project_id="my-project",
        ...     region="us-west1",
        ...     job_name="validibot-validator-backend-energyplus",
        ...     input_uri="gs://my-bucket/runs/abc-123/input.json",
        ... )
        >>> print(f"Started execution: {execution_name}")
    """
    # Allow either short job name ("my-job") or fully-qualified path.
    # If fully-qualified is provided, prefer that to avoid mismatch with args.
    job_path: str
    if job_name.startswith("projects/"):
        job_path = job_name
    else:
        job_path = f"projects/{project_id}/locations/{region}/jobs/{job_name}"

    client = run_v2.JobsClient()

    request = run_v2.RunJobRequest(
        name=job_path,
        overrides=run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    # Cloud Run Jobs only lets us pass run-time inputs via env
                    # overrides (no CLI args). We keep the rest of the contract
                    # in GCS (input_uri) to avoid large payloads in requests.
                    env=[
                        run_v2.EnvVar(name="VALIDIBOT_INPUT_URI", value=input_uri),
                    ],
                ),
            ],
        ),
    )

    logger.info(
        "Starting Cloud Run Job: %s with VALIDIBOT_INPUT_URI=%s", job_name, input_uri
    )

    # run_job returns a long-running operation. Do NOT call operation.result()
    # which would block until the job completes (potentially minutes/hours).
    # Instead, extract the execution name from the operation metadata and
    # return immediately. Job completion is handled via callbacks.
    operation: Operation = client.run_job(request=request)

    # The operation metadata contains the execution info. Access it directly
    # without blocking. If metadata is missing, fall back to a short wait on
    # the operation to populate the Execution resource.
    execution_name = getattr(operation.metadata, "name", None)
    if not execution_name:
        try:
            execution = operation.result(timeout=30)
            execution_name = execution.name
        except Exception as exc:
            logger.exception(
                "Cloud Run Job started but execution name unavailable; "
                "operation metadata missing. Operation: %s",
                getattr(operation, "operation", None),
            )
            msg = "Cloud Run Job started but execution name not available in metadata"
            raise RuntimeError(msg) from exc

    logger.info("Started execution: %s", execution_name)

    return execution_name


def get_execution_image_digest(execution_name: str) -> str | None:
    """Resolve the validator backend image reference from a Cloud Run Execution.

    Trust ADR Phase 5 Session A — captures the content-addressed
    identifier of the validator backend image that ran (or is running)
    inside a Cloud Run Job execution.

    Cloud Run exposes the configured image of an executing Job via
    ``execution.template.containers[0].image``. The string is
    whatever the Job spec was deployed with:

    - If the operator pinned the Job to a digest
      (``gcr.io/.../image@sha256:...``), this function returns that
      reference verbatim — a verifier can re-pull and confirm.
    - If the operator deployed the Job pinned to a tag
      (``gcr.io/.../image:v1``), this function returns the tag
      reference. We do NOT resolve the tag to a digest separately
      (that would require a registry round-trip with auth concerns);
      instead, the Phase 5 Session B ``VALIDATOR_BACKEND_IMAGE_POLICY``
      gate is what enforces "digest-pinned only" deployments.

    Returns ``None`` when the Execution metadata cannot be fetched or
    has no container image. Digest capture must never break a run, so
    every failure mode is logged at debug level and yields ``None``.
    """
    try:
        client = run_v2.ExecutionsClient()
        execution = client.get_execution(name=execution_name)
        containers = getattr(getattr(execution, "template", None), "containers", None)
        if not containers:
            return None
        image = getattr(containers[0], "image", None)
        return str(image) if image else None
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "Could not resolve image digest for execution %s: %s",
            execution_name,
            exc,
        )
        return None


def get_execution_status(execution_name: str) -> dict:
    """
    Get the status of a Cloud Run Job execution.

    Args:
        execution_name: Full execution name (returned from run_validator_job)

    Returns:
        Dictionary with execution status information

    Raises:
        google.api_core.exceptions.GoogleAPICallError: If status check fails

    Example:
        >>> status = get_execution_status(execution_name)
        >>> print(status["completion_status"])  # SUCCEEDED, FAILED, CANCELLED, etc.
    """
    client = run_v2.ExecutionsClient()
    execution = client.get_execution(name=execution_name)

    # Map the condition to a simple status
    completion_status: CloudRunJobStatus | None = None
    for condition in execution.conditions:
        if condition.type_ == "Completed":
            if condition.state == run_v2.Condition.State.CONDITION_SUCCEEDED:
                completion_status = CloudRunJobStatus.SUCCEEDED
            elif condition.state == run_v2.Condition.State.CONDITION_FAILED:
                completion_status = CloudRunJobStatus.FAILED
            break

    # If no completion condition yet, infer running/pending
    if completion_status is None:
        if execution.start_time and not execution.completion_time:
            completion_status = CloudRunJobStatus.RUNNING
        else:
            completion_status = CloudRunJobStatus.PENDING

    return {
        "name": execution.name,
        "job": execution.job,
        "create_time": execution.create_time,
        "start_time": execution.start_time,
        "completion_time": execution.completion_time,
        "completion_status": completion_status,
        "failed_count": execution.failed_count,
        "succeeded_count": execution.succeeded_count,
        "running_count": execution.running_count,
    }
