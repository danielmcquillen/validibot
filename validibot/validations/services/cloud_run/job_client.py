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
import time
from functools import lru_cache
from typing import TYPE_CHECKING

from google.cloud import run_v2

from validibot.validations.constants import CloudRunJobStatus

if TYPE_CHECKING:
    from google.api_core.operation import Operation

    from validibot.validations.services.cloud_run.gcs_runtime_capabilities import (
        AttemptGCSRuntimeCapability,
    )

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_cloud_run_jobs_client():
    """Return one lazy Cloud Run Jobs client per application process."""
    started = time.perf_counter()
    client = run_v2.JobsClient()
    logger.info(
        "Initialized shared Cloud Run Jobs client",
        extra={
            "gcp_client": "JobsClient",
            "client_initialization_ms": (time.perf_counter() - started) * 1000,
        },
    )
    return client


@lru_cache(maxsize=1)
def get_cloud_run_executions_client():
    """Return one lazy Cloud Run Executions client per application process."""
    started = time.perf_counter()
    client = run_v2.ExecutionsClient()
    logger.info(
        "Initialized shared Cloud Run Executions client",
        extra={
            "gcp_client": "ExecutionsClient",
            "client_initialization_ms": (time.perf_counter() - started) * 1000,
        },
    )
    return client


def clear_cloud_run_client_caches() -> None:
    """Clear process client caches for tests or a settings reset."""
    get_cloud_run_jobs_client.cache_clear()
    get_cloud_run_executions_client.cache_clear()


def run_validator_job(
    *,
    project_id: str,
    region: str,
    job_name: str,
    input_uri: str,
    gcs_capability: AttemptGCSRuntimeCapability | None = None,
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

    client = get_cloud_run_jobs_client()

    environment = [
        run_v2.EnvVar(name="VALIDIBOT_INPUT_URI", value=input_uri),
    ]
    if gcs_capability is not None:
        environment.extend(
            run_v2.EnvVar(name=name, value=value)
            for name, value in gcs_capability.as_environment().items()
        )

    request = run_v2.RunJobRequest(
        name=job_path,
        overrides=run_v2.RunJobRequest.Overrides(
            container_overrides=[
                run_v2.RunJobRequest.Overrides.ContainerOverride(
                    # Cloud Run Jobs only lets us pass run-time inputs via env
                    # overrides (no CLI args). We keep the rest of the contract
                    # in GCS (input_uri) to avoid large payloads in requests.
                    env=environment,
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
    rpc_started = time.perf_counter()
    try:
        operation: Operation = client.run_job(request=request)
    except Exception:
        logger.warning(
            "Cloud Run Job dispatch failed",
            extra={
                "gcp_client": "JobsClient",
                "gcp_operation": "run_job",
                "gcp_rpc_duration_ms": (time.perf_counter() - rpc_started) * 1000,
                "provider_resource_name": job_path,
            },
            exc_info=True,
        )
        raise
    logger.info(
        "Cloud Run Job dispatch accepted",
        extra={
            "gcp_client": "JobsClient",
            "gcp_operation": "run_job",
            "gcp_rpc_duration_ms": (time.perf_counter() - rpc_started) * 1000,
            "provider_resource_name": job_path,
        },
    )

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


def get_job_configured_image(
    *,
    project_id: str,
    region: str,
    job_name: str,
) -> str | None:
    """Resolve the *configured* container image of a Cloud Run Job.

    Trust ADR Phase 5 Session B — used by the policy gate to inspect
    a Job's image reference *before* triggering an Execution. Unlike
    :func:`get_execution_image_digest` (which reads from a started
    Execution), this fetches the Job spec itself so the policy can
    refuse to enqueue runs against floating-tag images.

    Returns the first container's image string verbatim — could be a
    tag reference (``gcr.io/.../image:v1``) or a digest reference
    (``gcr.io/.../image@sha256:...``) depending on how the Job was
    deployed. Returns ``None`` when the Job can't be fetched (the
    runner falls back to launching anyway; the launch itself will
    fail with a clearer error and the doctor command flags the
    misconfiguration separately).

    Arguments mirror :func:`run_validator_job` so the launcher can
    reuse the same project/region/job_name triple.
    """
    job_path: str
    if job_name.startswith("projects/"):
        job_path = job_name
    else:
        job_path = f"projects/{project_id}/locations/{region}/jobs/{job_name}"

    try:
        client = get_cloud_run_jobs_client()
        rpc_started = time.perf_counter()
        job = client.get_job(name=job_path)
        logger.debug(
            "Cloud Run Job metadata lookup completed",
            extra={
                "gcp_client": "JobsClient",
                "gcp_operation": "get_job",
                "gcp_rpc_duration_ms": (time.perf_counter() - rpc_started) * 1000,
                "provider_resource_name": job_path,
            },
        )
        containers = getattr(getattr(job, "template", None), "template", None)
        # Cloud Run Job structure: Job.template (ExecutionTemplate) →
        # template (TaskTemplate) → containers[0].image
        containers = getattr(containers, "containers", None)
        if not containers:
            return None
        image = getattr(containers[0], "image", None)
        return str(image) if image else None
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "Could not fetch configured image for Cloud Run job %s: %s",
            job_name,
            exc,
        )
        return None


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
    - If an unmanaged or test Job was deployed with a tag
      (``gcr.io/.../image:v1``), this function returns the tag
      reference. We do NOT resolve the tag to a digest separately
      (that would require a registry round-trip with auth concerns);
      the configured image-policy gate decides whether dispatch may proceed.

    Returns ``None`` when the Execution metadata cannot be fetched or
    has no container image. Digest capture must never break a run, so
    every failure mode is logged at debug level and yields ``None``.
    """
    try:
        client = get_cloud_run_executions_client()
        rpc_started = time.perf_counter()
        execution = client.get_execution(name=execution_name)
        logger.debug(
            "Cloud Run execution metadata lookup completed",
            extra={
                "gcp_client": "ExecutionsClient",
                "gcp_operation": "get_execution",
                "gcp_rpc_duration_ms": (time.perf_counter() - rpc_started) * 1000,
                "provider_execution_id": execution_name,
            },
        )
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
    client = get_cloud_run_executions_client()
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
