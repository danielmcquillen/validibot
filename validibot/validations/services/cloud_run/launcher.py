"""
Cloud Run Job launcher service.

This module orchestrates the complete flow of triggering Cloud Run Jobs:
1. Build typed input envelope
2. Upload submission and envelope to GCS
3. Trigger Cloud Run Job directly via Jobs API
4. Return ValidationResult with pending status

Architecture:
    Web -> Cloud Run Job (this module) -> Callback to worker

Design: Simple function-based orchestration. No complex state management.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ValidationError
from validibot_shared.fmu.envelopes import FMUInputEnvelope

from validibot.validations.constants import CloudRunJobStatus
from validibot.validations.constants import Severity
from validibot.validations.services.cloud_run.envelope_builder import (
    _resolve_step_resources,
)
from validibot.validations.services.cloud_run.envelope_builder import (
    build_energyplus_input_envelope,
)
from validibot.validations.services.cloud_run.gcs_client import upload_envelope
from validibot.validations.services.cloud_run.gcs_client import upload_envelope_local
from validibot.validations.services.cloud_run.gcs_client import upload_file
from validibot.validations.services.cloud_run.job_client import run_validator_job
from validibot.validations.services.fmu_bindings import resolve_input_value
from validibot.validations.utils.idf_template import (
    merge_and_validate_template_parameters,
)
from validibot.validations.utils.idf_template import substitute_template_parameters
from validibot.validations.validators.base import ValidationIssue
from validibot.validations.validators.base import ValidationResult
from validibot.workflows.step_configs import get_step_config

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

    In Cloud Run, validator containers POST their results back to Django via the
    worker service's internal API. We keep this separate from `SITE_URL` so that
    production can use a custom public domain (e.g., `validibot.com`) while
    callbacks still route to the IAM-protected worker `*.run.app` URL.

    Returns:
        Fully-qualified callback URL, e.g.
        `https://validibot-worker-xyz.a.run.app/api/v1/validation-callbacks/`.

    Raises:
        ValueError: If neither WORKER_URL nor SITE_URL is set.
    """
    worker_url = (getattr(settings, "WORKER_URL", "") or "").strip()
    if worker_url:
        base_url = worker_url
    else:
        base_url = (getattr(settings, "SITE_URL", "") or "").strip()
        logger.warning(
            (
                "WORKER_URL is not set; falling back to SITE_URL=%s for validation "
                "callbacks. In Cloud Run multi-service deployments this is usually "
                "incorrect."
            ),
            base_url,
        )

    if not base_url:
        msg = (
            "WORKER_URL (preferred) or SITE_URL must be set to build validation "
            "callback URL."
        )
        raise ValueError(msg)

    return f"{base_url.rstrip('/')}{VALIDATION_CALLBACK_PATH}"


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

    Supports two modes:

    **Direct mode** (existing): The submission is a complete IDF/epJSON
    file, uploaded directly to GCS for the container to run.

    **Template mode** (new): The step has a ``MODEL_TEMPLATE`` resource
    containing an IDF with ``$VARIABLE_NAME`` placeholders.  The
    submission is a JSON dict of parameter values.  This function merges
    the values with author defaults, validates constraints, substitutes
    into the template, and uploads the resolved IDF.  The container
    receives a normal IDF with no template awareness.

    Args:
        run: ValidationRun instance (already created in PENDING status)
        validator: Validator instance
        submission: Submission instance with IDF/epJSON or JSON params
        ruleset: Ruleset instance (may be None for EnergyPlus)
        step: WorkflowStep instance with config including weather_file

    Returns:
        ValidationResult with passed=None (pending), issues=[], stats
        with job info.  In template mode, stats also include
        ``template_parameters_used``, ``template_warnings``, and
        ``template_original_uri``.

    Raises:
        ValueError: If required config is missing (e.g., weather_file)
        Exception: If GCS upload or job trigger fails
    """
    try:
        # 0. Idempotency check: if this step run already has job info in output,
        # a job was already launched (possibly on a previous retry). Return
        # pending result to wait for the existing job's callback.
        current_step_run = run.current_step_run
        if not current_step_run:
            msg = f"No active step run found for ValidationRun {run.id}"
            raise ValueError(msg)  # noqa: TRY301

        existing_output = current_step_run.output or {}
        if existing_output.get("job_name"):
            logger.info(
                "Job already launched for step run %s (job=%s), skipping relaunch",
                current_step_run.id,
                existing_output.get("job_name"),
            )
            return ValidationResult(
                passed=None,  # Still pending
                issues=[],
                stats=existing_output,
            )

        # 1. Build GCS paths
        org_id = str(run.org.id)
        run_id = str(run.id)
        execution_bundle_uri = (
            f"gs://{settings.GCS_VALIDATION_BUCKET}/runs/{org_id}/{run_id}"
        )
        input_envelope_uri = f"{execution_bundle_uri}/input.json"

        # 2. Detect template mode via relational step_resources
        template_resource = step.step_resources.filter(
            role="MODEL_TEMPLATE",
        ).first()

        template_metadata: dict[str, object] = {}

        if template_resource:
            # ── Template mode ─────────────────────────────────────
            # Submission is JSON params; IDF comes from the template
            # resource.  Merge, validate, substitute, and upload the
            # resolved IDF.
            model_file_uri, template_metadata = _handle_template_mode(
                template_resource=template_resource,
                submission=submission,
                step=step,
                execution_bundle_uri=execution_bundle_uri,
            )
        else:
            # ── Direct mode ───────────────────────────────────────
            # Submission is a complete IDF/epJSON, uploaded directly.
            model_file_uri = f"{execution_bundle_uri}/model.epjson"
            logger.info("Uploading submission to %s", model_file_uri)
            upload_file(
                content=submission.get_content().encode("utf-8"),
                uri=model_file_uri,
                content_type="application/json",
            )

        # 3. Build callback URL and idempotency key
        callback_url = build_validation_callback_url()
        callback_id = f"step-run-{current_step_run.id}"

        # 4. Build typed input envelope
        step_config = step.config or {}
        timestep_per_hour = step_config.get("timestep_per_hour", 4)
        resource_files = _resolve_step_resources(step, role="WEATHER_FILE")

        envelope = build_energyplus_input_envelope(
            run_id=run_id,
            validator=validator,
            org_id=org_id,
            org_name=run.org.name,
            workflow_id=str(run.workflow.id),
            step_id=str(step.id),
            step_name=step.name,
            model_file_uri=model_file_uri,
            resource_files=resource_files,
            callback_url=callback_url,
            callback_id=callback_id,
            execution_bundle_uri=execution_bundle_uri,
            timestep_per_hour=timestep_per_hour,
        )

        # 5. Upload envelope to GCS
        logger.info("Uploading input envelope to %s", input_envelope_uri)
        upload_envelope(envelope, input_envelope_uri)

        # 6. Trigger Cloud Run Job directly via Jobs API
        job_name = settings.GCS_ENERGYPLUS_JOB_NAME

        logger.info("Triggering Cloud Run Job: %s", job_name)
        execution_name = run_validator_job(
            project_id=settings.GCP_PROJECT_ID,
            region=settings.GCP_REGION,
            job_name=job_name,
            input_uri=input_envelope_uri,
        )

        # 6.5. Update step run status to RUNNING
        from datetime import UTC
        from datetime import datetime

        from validibot.validations.constants import StepStatus

        now = datetime.now(UTC)
        current_step_run.status = StepStatus.RUNNING
        current_step_run.started_at = now
        current_step_run.save(update_fields=["status", "started_at"])

        logger.info(
            "Marked step run %s as RUNNING for run %s",
            current_step_run.id,
            run.id,
        )

        # 7. Return pending ValidationResult
        stats = {
            "job_status": CloudRunJobStatus.PENDING,
            "job_name": job_name,
            "execution_name": execution_name,
            "input_uri": input_envelope_uri,
            "execution_bundle_uri": execution_bundle_uri,
            **template_metadata,
        }

        return ValidationResult(
            passed=None,  # Pending - will be updated by callback
            issues=[],
            stats=stats,
        )

    except ValidationError as exc:
        # Template parameter validation failed — surface as user-friendly errors
        logger.warning("Template parameter validation failed: %s", exc.messages)
        issues = [
            ValidationIssue(
                path="template_parameters",
                message=msg,
                severity=Severity.ERROR,
            )
            for msg in exc.messages
        ]
        return ValidationResult(passed=False, issues=issues, stats={})

    except Exception as e:
        logger.exception("Failed to launch EnergyPlus Cloud Run Job")
        issues = [
            ValidationIssue(
                path="",
                message=f"Failed to launch EnergyPlus validation: {e!s}",
                severity=Severity.ERROR,
            ),
        ]
        return ValidationResult(passed=False, issues=issues, stats={})


def _handle_template_mode(
    *,
    template_resource,
    submission: Submission,
    step: WorkflowStep,
    execution_bundle_uri: str,
) -> tuple[str, dict[str, object]]:
    """Handle template mode: merge params, substitute, upload resolved IDF.

    Reads the template from the step-owned resource file, parses the
    submission as JSON parameter values, merges with author defaults,
    validates constraints, substitutes into the template, and uploads
    both the resolved IDF and the original template to GCS.

    Returns:
        Tuple of (model_file_uri, template_metadata).

    Raises:
        ValidationError: If parameter validation fails.
        ValueError: If template substitution fails.
    """
    # Read the template IDF from the step-owned resource
    template_content = template_resource.step_resource_file.read().decode("utf-8")
    # Reset file pointer in case it's read again later
    template_resource.step_resource_file.seek(0)

    # Parse submission as flat JSON parameter dict
    submitter_params = json.loads(submission.get_content())

    # Get typed step config for template variable definitions
    typed_config = get_step_config(step)

    # Merge submitter values with author defaults and validate
    merge_result = merge_and_validate_template_parameters(
        submitter_params=submitter_params,
        template_variables=typed_config.template_variables,
        case_sensitive=typed_config.case_sensitive,
    )

    # Perform template substitution — resolve $VARIABLES into values
    resolved_idf = substitute_template_parameters(
        idf_text=template_content,
        parameters=merge_result.parameters,
        case_sensitive=typed_config.case_sensitive,
    )

    # Upload resolved IDF as the model file
    model_file_uri = f"{execution_bundle_uri}/model.idf"
    logger.info("Uploading resolved template IDF to %s", model_file_uri)
    upload_file(
        content=resolved_idf.encode("utf-8"),
        uri=model_file_uri,
        content_type="text/plain",
    )

    # Upload original template for audit trail
    template_artifact_uri = f"{execution_bundle_uri}/template_original.idf"
    logger.info("Uploading original template to %s", template_artifact_uri)
    upload_file(
        content=template_content.encode("utf-8"),
        uri=template_artifact_uri,
        content_type="text/plain",
    )

    template_metadata: dict[str, object] = {
        "template_parameters_used": merge_result.parameters,
        "template_warnings": merge_result.warnings,
        "template_original_uri": template_artifact_uri,
    }

    return model_file_uri, template_metadata


def launch_fmu_validation(
    *,
    run: ValidationRun,
    validator: Validator,
    submission,
    ruleset,
    step: WorkflowStep,
) -> ValidationResult:
    """
    Launch an FMU validation via Cloud Run Jobs.

    Resolves inputs using catalog binding paths (or slug-name defaults), builds
    an FMU input envelope, uploads it, triggers the Cloud Run Job, and returns
    a pending ValidationResult.
    """
    try:
        # 0. Idempotency check: if this step run already has job info in output,
        # a job was already launched (possibly on a previous retry). Return
        # pending result to wait for the existing job's callback.
        current_step_run = run.current_step_run
        if not current_step_run:
            msg = f"No active step run for run {run.id}"
            raise ValueError(msg)  # noqa: TRY301

        existing_output = current_step_run.output or {}
        if existing_output.get("job_name"):
            logger.info(
                "Job already launched for step run %s (job=%s), skipping relaunch",
                current_step_run.id,
                existing_output.get("job_name"),
            )
            return ValidationResult(
                passed=None,  # Still pending
                issues=[],
                stats=existing_output,
            )

        fmu_model = validator.fmu_model
        if not fmu_model:
            msg = "FMU validator is missing an FMU model."
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
            from pathlib import Path

            base_dir = Path(settings.MEDIA_ROOT) / "files" / "runs" / org_id / run_id
            base_dir.mkdir(parents=True, exist_ok=True)
            execution_bundle_uri = str(base_dir)
            input_envelope_uri = str(base_dir / "input.json")

        # Resolve inputs based on catalog data paths
        submission_payload = (
            submission.get_content() if hasattr(submission, "get_content") else ""
        )
        if isinstance(submission_payload, str):
            # Best effort: parse JSON when possible
            import json

            try:
                submission_payload = json.loads(submission_payload)
            except Exception:
                submission_payload = {}
        input_values: dict[str, object] = {}
        for entry in validator.catalog_entries.filter(run_stage="INPUT"):
            slug = entry.slug
            value = resolve_input_value(
                submission_payload,
                data_path=(entry.target_data_path or "").strip(),
                slug=slug,
            )
            if value is None and entry.is_required:
                msg = f"Missing required input '{slug}' for FMU validator."
                raise ValueError(msg)  # noqa: TRY301
            if value is not None:
                input_values[slug] = value

        callback_url = build_validation_callback_url()

        # Generate deterministic idempotency key for callback deduplication.
        # Using the step run ID ensures that retries use the same callback_id,
        # allowing the callback receipt fencing to correctly identify and handle
        # duplicate callbacks. Note: Job launch idempotency is handled by the
        # check at step 0 above (checking step_run.output for existing job_name).
        callback_id = f"step-run-{current_step_run.id}"

        # Build envelope
        envelope = FMUInputEnvelope(
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
                "callback_id": callback_id,
                "callback_url": callback_url,
                "execution_bundle_uri": execution_bundle_uri,
            },
        )

        # Upload envelope
        if input_envelope_uri.startswith("gs://"):
            upload_envelope(envelope, input_envelope_uri)
        else:
            from pathlib import Path

            upload_envelope_local(envelope, Path(input_envelope_uri))

        # Trigger Cloud Run Job directly via Jobs API
        job_name = settings.GCS_FMU_JOB_NAME
        if not job_name:
            msg = "GCS_FMU_JOB_NAME is not configured."
            raise ValueError(msg)  # noqa: TRY301

        execution_name = run_validator_job(
            project_id=settings.GCP_PROJECT_ID,
            region=settings.GCP_REGION,
            job_name=job_name,
            input_uri=input_envelope_uri,
        )

        # Mark step run as RUNNING
        # Note: run.status and run.started_at are already set by
        # execute_workflow_steps() when
        # the run transitions from PENDING to RUNNING. We only need to mark the
        # step run as running here.
        from datetime import UTC
        from datetime import datetime

        from validibot.validations.constants import StepStatus

        now = datetime.now(UTC)
        current_step_run.status = StepStatus.RUNNING
        current_step_run.started_at = now
        current_step_run.save(update_fields=["status", "started_at"])

        stats = {
            "job_status": CloudRunJobStatus.PENDING,
            "job_name": job_name,
            "execution_name": execution_name,
            "input_uri": input_envelope_uri,
            "execution_bundle_uri": execution_bundle_uri,
            "signals": {},  # populated on callback; reserved for downstream steps
        }
        return ValidationResult(passed=None, issues=[], stats=stats)

    except Exception as e:
        logger.exception("Failed to launch FMU Cloud Run Job")
        issues = [
            ValidationIssue(
                path="",
                message=f"Failed to launch FMU validation: {e!s}",
                severity=Severity.ERROR,
            ),
        ]
        return ValidationResult(passed=False, issues=issues, stats={})
