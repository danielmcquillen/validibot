"""
Unified container launcher for advanced validators.

This module provides a unified interface for launching container-based validators
(EnergyPlus, FMI, etc.) that works across different deployment targets:

- Self-hosted: Docker containers via local socket (synchronous)
- GCP: Google Cloud Run Jobs (async with callbacks)
- AWS: AWS Batch (future)

## Execution Model

For synchronous runners (Docker), the launcher:
1. Builds the input envelope for the validator
2. Uploads the envelope and submission files to storage
3. Runs the container and **waits for completion**
4. Downloads and processes the output envelope
5. Returns the final ValidationResult

For async runners (Cloud Run Jobs), the launcher:
1-3. Same as above
4. Returns a pending result immediately
5. Results are delivered via HTTP callback when the job completes

Usage:
    from validibot.validations.services.container_launcher import launch_validation

    result = launch_validation(
        run=validation_run,
        validator=validator,
        submission=submission,
        step=workflow_step,
    )
    # For sync runners: result.passed is True/False
    # For async runners: result.passed is None (pending)
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from django.conf import settings

from validibot.core.storage import get_data_storage
from validibot.validations.constants import Severity
from validibot.validations.constants import StepStatus
from validibot.validations.engines.base import ValidationIssue
from validibot.validations.engines.base import ValidationResult
from validibot.validations.services.runners import ExecutionResult
from validibot.validations.services.runners import get_validator_runner

if TYPE_CHECKING:
    from validibot.submissions.models import Submission
    from validibot.validations.models import Ruleset
    from validibot.validations.models import ValidationRun
    from validibot.validations.models import Validator
    from validibot.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)

VALIDATION_CALLBACK_PATH = "/api/v1/validation-callbacks/"


def build_validation_callback_url() -> str:
    """
    Build the fully-qualified validation callback URL.

    Returns the URL where validator containers should POST their results.
    Uses WORKER_URL if set (for multi-service deployments), otherwise SITE_URL.

    Returns:
        Fully-qualified callback URL.

    Raises:
        ValueError: If neither WORKER_URL nor SITE_URL is set.
    """
    worker_url = (getattr(settings, "WORKER_URL", "") or "").strip()
    if worker_url:
        base_url = worker_url
    else:
        base_url = (getattr(settings, "SITE_URL", "") or "").strip()
        if worker_url == "":  # Only warn if WORKER_URL is empty, not just missing
            logger.warning(
                "WORKER_URL is not set; using SITE_URL=%s for validation callbacks",
                base_url,
            )

    if not base_url:
        msg = "WORKER_URL or SITE_URL must be set to build validation callback URL."
        raise ValueError(msg)

    return f"{base_url.rstrip('/')}{VALIDATION_CALLBACK_PATH}"


def get_container_image(validator: Validator) -> str:
    """
    Get the container image for a validator.

    Maps validator types to their container images. In production, these
    should come from a registry (GCR, ECR, Docker Hub).

    Args:
        validator: Validator instance

    Returns:
        Container image name with tag
    """
    # Default image naming convention: validibot-validator-{type}
    # This can be overridden by validator config in the future
    image_name = f"validibot-validator-{validator.validation_type}"

    # Get image tag from settings or use "latest"
    image_tag = getattr(settings, "VALIDATOR_IMAGE_TAG", "latest")

    # Get registry prefix if configured
    registry = getattr(settings, "VALIDATOR_IMAGE_REGISTRY", "")
    if registry:
        return f"{registry}/{image_name}:{image_tag}"

    return f"{image_name}:{image_tag}"


def launch_validation(
    *,
    run: ValidationRun,
    validator: Validator,
    submission: Submission,
    ruleset: Ruleset | None,
    step: WorkflowStep,
) -> ValidationResult:
    """
    Launch a container-based validation.

    This is the unified entry point for launching advanced validators. It:
    1. Checks for idempotency (skips if already launched)
    2. Uploads submission to storage
    3. Builds and uploads input envelope
    4. Triggers container via the configured runner
    5. For sync runners: waits for completion and processes output
    6. For async runners: returns pending result

    Args:
        run: ValidationRun instance (already created in PENDING status)
        validator: Validator instance
        submission: Submission instance with content to validate
        ruleset: Ruleset instance (may be None for some validator types)
        step: WorkflowStep instance with validation configuration

    Returns:
        ValidationResult with passed=True/False (for sync runners),
        or passed=None (pending, for async runners)
    """
    try:
        # 0. Idempotency check
        current_step_run = run.current_step_run
        if not current_step_run:
            msg = f"No active step run found for ValidationRun {run.id}"
            raise ValueError(msg)  # noqa: TRY301

        existing_output = current_step_run.output or {}
        if existing_output.get("execution_id"):
            logger.info(
                "Container already launched for step run %s (execution=%s)",
                current_step_run.id,
                existing_output.get("execution_id"),
            )
            return ValidationResult(
                passed=None,
                issues=[],
                stats=existing_output,
            )

        # 1. Get storage and runner
        storage = get_data_storage()
        runner = get_validator_runner()

        # 2. Build paths
        org_id = str(run.org.id)
        run_id = str(run.id)
        base_path = f"runs/{org_id}/{run_id}"
        input_envelope_path = f"{base_path}/input.json"
        output_envelope_path = f"{base_path}/output.json"
        submission_path = f"{base_path}/submission"

        # 3. Upload submission file
        submission_content = submission.get_content()
        if isinstance(submission_content, str):
            submission_bytes = submission_content.encode("utf-8")
        else:
            submission_bytes = submission_content
        storage.write(submission_path, submission_bytes)
        submission_uri = storage.get_uri(submission_path)

        # 4. Build callback URL and ID (still needed for envelope, even if unused)
        callback_url = build_validation_callback_url()
        callback_id = f"step-run-{current_step_run.id}"

        # 5. Build execution bundle URI (directory for this run's files)
        execution_bundle_uri = storage.get_uri(base_path)
        output_envelope_uri = storage.get_uri(output_envelope_path)

        # 6. Build and upload input envelope
        # Import here to avoid circular dependency
        from validibot.validations.services.cloud_run.envelope_builder import (
            build_input_envelope,
        )

        # Temporarily set step config with URIs for the envelope builder
        # TODO: Refactor envelope builder to accept URIs directly
        step_config = step.config or {}
        step.config = {
            **step_config,
            "primary_file_uri": submission_uri,
            # Weather file URI should already be in step config for EnergyPlus
        }

        try:
            envelope = build_input_envelope(
                run=run,
                callback_url=callback_url,
                callback_id=callback_id,
                execution_bundle_uri=execution_bundle_uri,
            )
        finally:
            # Restore original config
            step.config = step_config

        # Upload envelope to storage
        input_envelope_uri = storage.get_uri(input_envelope_path)
        storage.write_envelope(input_envelope_path, envelope)

        logger.info("Uploaded input envelope to %s", input_envelope_uri)

        # 7. Update step run status to RUNNING
        now = datetime.now(UTC)
        current_step_run.status = StepStatus.RUNNING
        current_step_run.started_at = now
        current_step_run.save(update_fields=["status", "started_at"])

        logger.info(
            "Marked step run %s as RUNNING for run %s",
            current_step_run.id,
            run.id,
        )

        # 8. Get container image and run
        container_image = get_container_image(validator)

        logger.info(
            "Launching container: image=%s, runner=%s",
            container_image,
            runner.get_runner_type(),
        )

        # Run container synchronously (blocks until completion)
        result: ExecutionResult = runner.run(
            container_image=container_image,
            input_uri=input_envelope_uri,
            output_uri=output_envelope_uri,
        )

        # 9. Process the result
        return _process_execution_result(
            result=result,
            run=run,
            validator=validator,
            storage=storage,
            output_envelope_path=output_envelope_path,
            container_image=container_image,
            input_envelope_uri=input_envelope_uri,
            execution_bundle_uri=execution_bundle_uri,
        )

    except TimeoutError as e:
        logger.exception("Container validation timed out")
        issues = [
            ValidationIssue(
                path="",
                message=f"Validation timed out: {e!s}",
                severity=Severity.ERROR,
            ),
        ]
        return ValidationResult(passed=False, issues=issues, stats={"error": "timeout"})

    except Exception as e:
        logger.exception("Failed to run container validation")
        issues = [
            ValidationIssue(
                path="",
                message=f"Failed to run validation: {e!s}",
                severity=Severity.ERROR,
            ),
        ]
        return ValidationResult(passed=False, issues=issues, stats={})


def _process_execution_result(
    *,
    result: ExecutionResult,
    run: ValidationRun,
    validator: Validator,
    storage,
    output_envelope_path: str,
    container_image: str,
    input_envelope_uri: str,
    execution_bundle_uri: str,
) -> ValidationResult:
    """
    Process the result of a synchronous container execution.

    Downloads and parses the output envelope, extracts issues and metrics,
    and returns a ValidationResult.

    Args:
        result: ExecutionResult from the runner
        run: ValidationRun instance
        validator: Validator instance
        storage: DataStorage instance
        output_envelope_path: Path where output envelope should be
        container_image: Container image that was run
        input_envelope_uri: URI of input envelope (for stats)
        execution_bundle_uri: URI of execution bundle directory (for stats)

    Returns:
        ValidationResult with passed=True/False based on container result
    """
    stats = {
        "execution_id": result.execution_id,
        "exit_code": result.exit_code,
        "duration_seconds": result.duration_seconds,
        "container_image": container_image,
        "input_uri": input_envelope_uri,
        "output_uri": result.output_uri,
        "execution_bundle_uri": execution_bundle_uri,
    }

    # Container failed
    if not result.succeeded:
        logger.warning(
            "Container failed for run %s: exit_code=%d, error=%s",
            run.id,
            result.exit_code,
            result.error_message,
        )
        error_msg = (
            result.error_message or f"Container exited with code {result.exit_code}"
        )
        issues = [
            ValidationIssue(
                path="",
                message=error_msg,
                severity=Severity.ERROR,
            ),
        ]
        # Include logs in stats for debugging
        if result.logs:
            stats["logs"] = result.logs[:10000]  # Truncate long logs
        return ValidationResult(passed=False, issues=issues, stats=stats)

    # Container succeeded - read and parse output envelope
    try:
        output_data = storage.read(output_envelope_path)
        if output_data is None:
            logger.error("Output envelope not found at %s", output_envelope_path)
            issues = [
                ValidationIssue(
                    path="",
                    message="Container completed but output envelope not found",
                    severity=Severity.ERROR,
                ),
            ]
            return ValidationResult(passed=False, issues=issues, stats=stats)

        import json
        output_envelope = json.loads(output_data)

    except Exception as e:
        logger.exception("Failed to read output envelope: %s", output_envelope_path)
        issues = [
            ValidationIssue(
                path="",
                message=f"Failed to read output envelope: {e!s}",
                severity=Severity.ERROR,
            ),
        ]
        return ValidationResult(passed=False, issues=issues, stats=stats)

    # Extract issues from output envelope
    issues = _extract_issues_from_envelope(output_envelope)

    # Extract metrics/signals if available
    outputs = output_envelope.get("outputs", {})
    if outputs:
        stats["outputs"] = outputs

    # Determine pass/fail based on envelope status and issues
    envelope_status = output_envelope.get("status", "unknown")
    has_errors = any(i.severity == Severity.ERROR for i in issues)

    if envelope_status == "success" and not has_errors:
        passed = True
    elif envelope_status == "error" or has_errors:
        passed = False
    else:
        # Warning-only results pass
        passed = True

    logger.info(
        "Container validation completed for run %s: passed=%s, issues=%d",
        run.id,
        passed,
        len(issues),
    )

    return ValidationResult(passed=passed, issues=issues, stats=stats)


def _extract_issues_from_envelope(output_envelope: dict) -> list[ValidationIssue]:
    """
    Extract ValidationIssue objects from an output envelope.

    Args:
        output_envelope: Parsed output envelope dict

    Returns:
        List of ValidationIssue objects
    """
    issues = []

    # Check for errors in envelope
    errors = output_envelope.get("errors", [])
    for error in errors:
        if isinstance(error, str):
            issues.append(
                ValidationIssue(path="", message=error, severity=Severity.ERROR)
            )
        elif isinstance(error, dict):
            issues.append(
                ValidationIssue(
                    path=error.get("path", ""),
                    message=error.get("message", str(error)),
                    severity=Severity(error.get("severity", "error")),
                )
            )

    # Check for warnings
    warnings = output_envelope.get("warnings", [])
    for warning in warnings:
        if isinstance(warning, str):
            issues.append(
                ValidationIssue(path="", message=warning, severity=Severity.WARNING)
            )
        elif isinstance(warning, dict):
            issues.append(
                ValidationIssue(
                    path=warning.get("path", ""),
                    message=warning.get("message", str(warning)),
                    severity=Severity.WARNING,
                )
            )

    # Check for issues array (standard format)
    envelope_issues = output_envelope.get("issues", [])
    for issue in envelope_issues:
        if isinstance(issue, dict):
            issues.append(
                ValidationIssue(
                    path=issue.get("path", ""),
                    message=issue.get("message", str(issue)),
                    severity=Severity(issue.get("severity", "error")),
                )
            )

    return issues
