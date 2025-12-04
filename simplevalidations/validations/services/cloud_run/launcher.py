"""
Cloud Run Job launcher service.

This module orchestrates the complete flow of triggering Cloud Run Jobs:
1. Build typed input envelope
2. Upload submission and envelope to GCS
3. Trigger Cloud Run Job via Cloud Tasks
4. Return ValidationResult with pending status

Design: Simple function-based orchestration. No complex state management.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings

from simplevalidations.validations.constants import Severity
from simplevalidations.validations.engines.base import ValidationIssue
from simplevalidations.validations.engines.base import ValidationResult
from simplevalidations.validations.services.cloud_run.envelope_builder import (
    build_energyplus_input_envelope,
)
from simplevalidations.validations.services.cloud_run.gcs_client import upload_envelope
from simplevalidations.validations.services.cloud_run.gcs_client import upload_file
from simplevalidations.validations.services.cloud_run.job_client import (
    trigger_validator_job,
)
from simplevalidations.validations.services.cloud_run.token_service import (
    create_callback_token,
)
from sv_shared.fmi.envelopes import FMIInputEnvelope
from sv_shared.fmi.envelopes import FMISimulationConfig
from sv_shared.validations.envelopes import ValidationInputEnvelope

if TYPE_CHECKING:

    from simplevalidations.submissions.models import Submission
    from simplevalidations.validations.models import Ruleset
    from simplevalidations.validations.models import ValidationRun
    from simplevalidations.validations.models import Validator
    from simplevalidations.workflows.models import WorkflowStep

logger = logging.getLogger(__name__)


def launch_energyplus_validation(
    *,
    run: ValidationRun,
    validator: Validator,
    submission: Submission,
    ruleset: Ruleset | None,
    step: WorkflowStep,
) -> ValidationResult:
    """
    Launch an EnergyPlus validation via Cloud Run Jobs.

    This function orchestrates the complete flow:
    1. Extract weather file from ruleset metadata
    2. Upload submission file to GCS
    3. Build typed input envelope
    4. Upload envelope to GCS
    5. Create callback JWT token
    6. Trigger Cloud Run Job via Cloud Tasks
    7. Return pending ValidationResult

    Args:
        run: ValidationRun instance (already created in PENDING status)
        validator: Validator instance
        submission: Submission instance with IDF/epJSON content
        ruleset: Ruleset instance with weather_file metadata
        step: WorkflowStep instance with config

    Returns:
        ValidationResult with passed=None (pending), issues=[], stats with job info

    Raises:
        ValueError: If required metadata is missing (e.g., weather_file)
        Exception: If GCS upload or job trigger fails

    Example:
        >>> result = launch_energyplus_validation(
        ...     run=validation_run,
        ...     validator=energyplus_validator,
        ...     submission=idf_submission,
        ...     ruleset=energyplus_ruleset,
        ...     step=workflow_step,
        ... )
        >>> assert result.passed is None  # Pending
        >>> assert "job_name" in result.stats
    """
    try:
        # 1. Extract weather file from ruleset metadata
        if not ruleset or not ruleset.metadata:
            msg = "EnergyPlus validations require a ruleset with weather_file metadata"
            raise ValueError(msg)  # noqa: TRY301

        weather_file = ruleset.metadata.get("weather_file")
        if not weather_file:
            msg = "Ruleset metadata must include 'weather_file' (e.g., 'USA_CA_SF.epw')"
            raise ValueError(msg)  # noqa: TRY301

        # 2. Build GCS paths
        org_id = str(run.org.id)
        run_id = str(run.id)
        execution_bundle_uri = (
            f"gs://{settings.GCS_VALIDATION_BUCKET}/runs/{org_id}/{run_id}"
        )
        model_file_uri = f"{execution_bundle_uri}/model.epjson"
        weather_file_uri = (
            f"gs://{settings.GCS_VALIDATION_BUCKET}/assets/weather/{weather_file}"
        )
        input_envelope_uri = f"{execution_bundle_uri}/input.json"

        # 3. Upload submission file to GCS
        # TODO: Detect file type (IDF vs epJSON) and use appropriate extension
        logger.info(f"Uploading submission to {model_file_uri}")
        upload_file(
            content=submission.content.encode("utf-8"),
            uri=model_file_uri,
            content_type="application/json",
        )

        # 4. Create callback token
        # Get the current step run to include in token claims
        current_step_run = run.current_step_run
        if not current_step_run:
            msg = f"No active step run found for ValidationRun {run.id}"
            raise ValueError(msg)  # noqa: TRY301

        callback_token = create_callback_token(
            run_id=run_id,
            step_run_id=str(current_step_run.id),
            validator_id=str(validator.id),
            org_id=org_id,
            kms_key_name=settings.GCS_CALLBACK_KMS_KEY,
            kms_key_version=getattr(settings, "GCS_CALLBACK_KMS_KEY_VERSION", None),
            expires_hours=24,
        )

        # 5. Build callback URL
        callback_url = f"{settings.SITE_URL}/api/v1/validation-callbacks/"

        # 6. Build typed input envelope
        step_config = step.config or {}
        timestep_per_hour = step_config.get("timestep_per_hour", 4)
        output_variables = step_config.get("output_variables")

        envelope = build_energyplus_input_envelope(
            run_id=run_id,
            validator=validator,
            org_id=org_id,
            org_name=run.org.name,
            workflow_id=str(run.workflow.id),
            step_id=str(step.id),
            step_name=step.name,
            model_file_uri=model_file_uri,
            weather_file_uri=weather_file_uri,
            callback_url=callback_url,
            callback_token=callback_token,
            execution_bundle_uri=execution_bundle_uri,
            timestep_per_hour=timestep_per_hour,
            output_variables=output_variables,
        )

        # 7. Upload envelope to GCS
        logger.info(f"Uploading input envelope to {input_envelope_uri}")
        upload_envelope(envelope, input_envelope_uri)

        # 8. Trigger Cloud Run Job via Cloud Tasks
        job_name = settings.GCS_ENERGYPLUS_JOB_NAME
        queue_name = settings.GCS_TASK_QUEUE_NAME

        logger.info(f"Triggering Cloud Run Job: {job_name}")
        task_name = trigger_validator_job(
            project_id=settings.GCP_PROJECT_ID,
            region=settings.GCP_REGION,
            queue_name=queue_name,
            job_name=job_name,
            input_uri=input_envelope_uri,
        )

        # 8.5. Update run and step run status to RUNNING
        from datetime import UTC
        from datetime import datetime

        from simplevalidations.validations.constants import StepStatus
        from simplevalidations.validations.constants import ValidationRunStatus

        now = datetime.now(UTC)
        run.status = ValidationRunStatus.RUNNING
        run.started_at = now
        run.save(update_fields=["status", "started_at"])

        current_step_run.status = StepStatus.RUNNING
        current_step_run.started_at = now
        current_step_run.save(update_fields=["status", "started_at"])

        logger.info(
            "Marked run %s and step run %s as RUNNING",
            run.id,
            current_step_run.id,
        )

        # 9. Return pending ValidationResult
        stats = {
            "status": "pending",
            "job_name": job_name,
            "task_name": task_name,
            "input_uri": input_envelope_uri,
            "execution_bundle_uri": execution_bundle_uri,
        }

        return ValidationResult(
            passed=None,  # Pending - will be updated by callback
            issues=[],
            stats=stats,
        )

    except Exception as e:
        logger.exception("Failed to launch EnergyPlus Cloud Run Job")
        # Return failure result
        issues = [
            ValidationIssue(
                path="",
                message=f"Failed to launch EnergyPlus validation: {e!s}",
                severity=Severity.ERROR,
            ),
        ]
        return ValidationResult(passed=False, issues=issues, stats={})


def launch_fmi_validation(
    *,
    run: "ValidationRun",
    validator: "Validator",
    submission,
    ruleset,
    step: "WorkflowStep",
) -> ValidationResult:
    """
    Launch an FMI validation via Cloud Run Jobs.

    Resolves inputs using catalog binding paths (or slug-name defaults), builds
    an FMI input envelope, uploads it, triggers the Cloud Run Job, and returns
    a pending ValidationResult.
    """
    try:
        fmu_model = validator.fmu_model
        if not fmu_model:
            msg = "FMI validator is missing an FMU model."
            raise ValueError(msg)  # noqa: TRY301

        # Determine FMU URI (cloud vs local)
        fmu_uri = fmu_model.gcs_uri or getattr(fmu_model.file, "path", "")
        if not fmu_uri:
            msg = "FMU has no storage URI; upload failed."
            raise ValueError(msg)  # noqa: TRY301

        org_id = str(run.org.id)
        run_id = str(run.id)
        if settings.GCS_VALIDATION_BUCKET:
            execution_bundle_uri = (
                f"gs://{settings.GCS_VALIDATION_BUCKET}/runs/{org_id}/{run_id}"
            )
            input_envelope_uri = f"{execution_bundle_uri}/input.json"
        else:
            # Local dev: store under media/files/runs/<org>/<run>/input.json
            base_dir = settings.BASE_DIR / "media" / "files" / "runs" / org_id / run_id
            base_dir.mkdir(parents=True, exist_ok=True)
            execution_bundle_uri = str(base_dir)
            input_envelope_uri = str(base_dir / "input.json")

        # Resolve inputs based on catalog binding paths
        submission_payload = submission.content if hasattr(submission, "content") else ""
        input_values: dict[str, object] = {}
        for entry in validator.catalog_entries.filter(run_stage="INPUT"):
            binding_path = (entry.input_binding_path or "").strip()
            slug = entry.slug
            value = None
            if binding_path:
                value = submission_payload.get(binding_path) if isinstance(submission_payload, dict) else None
            elif isinstance(submission_payload, dict):
                value = submission_payload.get(slug)
            if value is None and entry.is_required:
                msg = f"Missing required input '{slug}' for FMI validator."
                raise ValueError(msg)  # noqa: TRY301
            if value is not None:
                input_values[slug] = value

        # Build callback token
        current_step_run = run.current_step_run
        if not current_step_run:
            msg = f"No active step run for run {run.id}"
            raise ValueError(msg)  # noqa: TRY301
        callback_token = create_callback_token(
            run_id=run_id,
            step_run_id=str(current_step_run.id),
            validator_id=str(validator.id),
            org_id=org_id,
            kms_key_name=settings.GCS_CALLBACK_KMS_KEY,
        )
        callback_url = f"{settings.SITE_URL}/api/v1/validation-callbacks/"

        # Build envelope
        envelope = FMIInputEnvelope(
            run_id=run_id,
            validator={
                "id": str(validator.id),
                "type": validator.validation_type,
                "version": validator.version,
            },
            org={
                "id": org_id,
                "name": run.org.name,
            },
            workflow={
                "id": str(run.workflow.id),
                "step_id": str(step.id),
                "step_name": step.name,
            },
            input_files=[
                {
                    "name": "model.fmu",
                    "mime_type": "application/vnd.fmi.fmu",
                    "role": "fmu",
                    "uri": fmu_uri,
                }
            ],
            inputs={
                "input_values": input_values,
                "simulation": {},
                "output_variables": [],
            },
            context={
                "callback_url": callback_url,
                "callback_token": callback_token,
                "execution_bundle_uri": execution_bundle_uri,
            },
        )

        # Upload envelope
        if input_envelope_uri.startswith("gs://"):
            upload_envelope(envelope, input_envelope_uri)
        else:
            from pathlib import Path

            Path(input_envelope_uri).write_text(envelope.model_dump_json(indent=2))

        # Trigger Cloud Run Job
        job_name = settings.GCS_FMI_JOB_NAME
        queue_name = settings.GCS_TASK_QUEUE_NAME
        if not job_name:
            msg = "GCS_FMI_JOB_NAME is not configured."
            raise ValueError(msg)  # noqa: TRY301

        task_name = trigger_validator_job(
            project_id=settings.GCP_PROJECT_ID,
            region=settings.GCP_REGION,
            queue_name=queue_name,
            job_name=job_name,
            input_uri=input_envelope_uri,
        )

        # Mark run/step running
        from datetime import UTC, datetime

        from simplevalidations.validations.constants import StepStatus
        from simplevalidations.validations.constants import ValidationRunStatus

        now = datetime.now(UTC)
        run.status = ValidationRunStatus.RUNNING
        run.started_at = now
        run.save(update_fields=["status", "started_at"])
        current_step_run.status = StepStatus.RUNNING
        current_step_run.started_at = now
        current_step_run.save(update_fields=["status", "started_at"])

        stats = {
            "status": "pending",
            "job_name": job_name,
            "task_name": task_name,
            "input_uri": input_envelope_uri,
            "execution_bundle_uri": execution_bundle_uri,
        }
        return ValidationResult(passed=None, issues=[], stats=stats)

    except Exception as e:
        logger.exception("Failed to launch FMI Cloud Run Job")
        issues = [
            ValidationIssue(
                path="",
                message=f"Failed to launch FMI validation: {e!s}",
                severity=Severity.ERROR,
            ),
        ]
        return ValidationResult(passed=False, issues=issues, stats={})
