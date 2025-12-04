"""
Callback API endpoint for Cloud Run Job validators.

This module handles ValidationCallback POSTs from validator containers.
It verifies the JWT token, downloads the output envelope from GCS, and
updates the ValidationRun in the database.

Design: Simple APIView with clear error handling. No complex permissions.
"""

import logging
from collections import Counter
from datetime import UTC
from datetime import datetime

from django.conf import settings
from django.db.models import Count
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from sv_shared.energyplus.envelopes import EnergyPlusOutputEnvelope
from sv_shared.validations.envelopes import ValidationCallback
from sv_shared.validations.envelopes import ValidationStatus

from simplevalidations.validations.constants import Severity
from simplevalidations.validations.constants import StepStatus
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.models import ValidationFinding
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.models import ValidationRunSummary
from simplevalidations.validations.models import ValidationStepRun
from simplevalidations.validations.models import ValidationStepRunSummary
from simplevalidations.validations.services.cloud_run.gcs_client import (
    download_envelope,
)
from simplevalidations.validations.services.cloud_run.token_service import (
    verify_callback_token,
)

logger = logging.getLogger(__name__)


def _coerce_finished_at(finished_at_candidate) -> datetime:
    """Normalize finished_at to an aware datetime in UTC."""
    if finished_at_candidate is None:
        return datetime.now(tz=UTC)
    if isinstance(finished_at_candidate, datetime):
        dt_value = finished_at_candidate
    elif isinstance(finished_at_candidate, str):
        # Handle common ISO strings, including trailing Z
        iso_value = finished_at_candidate.replace("Z", "+00:00")
        try:
            dt_value = datetime.fromisoformat(iso_value)
        except ValueError:
            logger.warning(
                "Could not parse finished_at string '%s', defaulting to now",
                finished_at_candidate,
            )
            return datetime.now(tz=UTC)
    else:
        logger.warning(
            "Unexpected finished_at type %s, defaulting to now",
            type(finished_at_candidate),
        )
        return datetime.now(tz=UTC)

    if dt_value.tzinfo is None:
        dt_value = dt_value.replace(tzinfo=UTC)
    return dt_value


class ValidationCallbackView(APIView):
    """
    Handle validation completion callbacks from Cloud Run Jobs.

    This endpoint receives POSTs from validator containers when they finish
    executing. The callback contains minimal data (run_id, status, result_uri)
    and a JWT token for authentication.

    The endpoint:
    1. Verifies the JWT token
    2. Downloads the full output envelope from GCS
    3. Updates the ValidationRun in the database
    4. Returns 200 OK

    URL: /api/v1/validation-callbacks/
    Method: POST
    Authentication: JWT token in callback payload
    """

    # Allow unauthenticated access - we verify via JWT token in payload
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        """
        Handle validation callback from Cloud Run Job.

        Expected payload (ValidationCallback):
        {
            "callback_token": "jwt_token_here",
            "run_id": "abc-123",
            "status": "success",
            "result_uri": "gs://bucket/runs/abc-123/output.json"
        }
        """
        try:
            # Parse and validate callback payload
            callback = ValidationCallback.model_validate(request.data)

            logger.info(
                "Received callback for run %s with status %s",
                callback.run_id,
                callback.status,
            )

            # Verify JWT token
            try:
                kms_key_name = settings.GCS_CALLBACK_KMS_KEY
                kms_key_version = getattr(
                    settings,
                    "GCS_CALLBACK_KMS_KEY_VERSION",
                    None,
                )
                token_payload = verify_callback_token(
                    callback.callback_token,
                    kms_key_name,
                    kms_key_version,
                )
            except ValueError as e:
                logger.warning("Invalid callback token: %s", e)
                return Response(
                    {"error": "Invalid or expired token"},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            # Verify run_id in token matches callback
            if token_payload["run_id"] != callback.run_id:
                logger.warning(
                    "Token run_id mismatch: token=%s, callback=%s",
                    token_payload["run_id"],
                    callback.run_id,
                )
                return Response(
                    {"error": "Token run_id mismatch"},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            # Get the validation run
            try:
                run = ValidationRun.objects.get(id=callback.run_id)
            except ValidationRun.DoesNotExist:
                logger.warning("Validation run not found: %s", callback.run_id)
                return Response(
                    {"error": "Validation run not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            # Verify org claim
            if str(run.org_id) != str(token_payload.get("org_id")):
                logger.warning(
                    "Token org_id mismatch: token=%s, run=%s",
                    token_payload.get("org_id"),
                    run.org_id,
                )
                return Response(
                    {"error": "Token org mismatch"},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            # Load the specific step run referenced in the token
            try:
                step_run = ValidationStepRun.objects.select_related(
                    "workflow_step__validator",
                ).get(
                    id=token_payload.get("step_run_id"),
                    validation_run=run,
                )
            except ValidationStepRun.DoesNotExist:
                logger.warning(
                    "Step run not found or not linked to run: run=%s step_run=%s",
                    run.id,
                    token_payload.get("step_run_id"),
                )
                return Response(
                    {"error": "Step run not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            validator = step_run.workflow_step.validator
            if not validator:
                logger.error("No validator found for step run: %s", step_run.id)
                return Response(
                    {"error": "No validator found for step"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if str(validator.id) != str(token_payload.get("validator_id")):
                logger.warning(
                    "Token validator_id mismatch: token=%s, step=%s",
                    token_payload.get("validator_id"),
                    validator.id,
                )
                return Response(
                    {"error": "Token validator mismatch"},
                    status=status.HTTP_401_UNAUTHORIZED,
                )

            # Download the output envelope from GCS
            # Determine the envelope class based on validator type
            if validator.validation_type == "energyplus":
                envelope_class = EnergyPlusOutputEnvelope
            else:
                logger.error(
                    "Unsupported validator type: %s",
                    validator.validation_type,
                )
                error_msg = f"Unsupported validator type: {validator.validation_type}"
                return Response(
                    {"error": error_msg},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            try:
                output_envelope = download_envelope(
                    callback.result_uri,
                    envelope_class,
                )
            except Exception as e:
                logger.exception("Failed to download output envelope")
                return Response(
                    {"error": f"Failed to download output envelope: {e}"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            # Double-check the envelope matches the expected validator
            if str(output_envelope.validator.id) != str(validator.id):
                logger.warning(
                    "Envelope validator mismatch: envelope=%s expected=%s",
                    output_envelope.validator.id,
                    validator.id,
                )
                return Response(
                    {"error": "Validator mismatch in output envelope"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            if str(getattr(output_envelope.org, "id", "")) != str(run.org_id):
                logger.warning(
                    "Envelope org mismatch: envelope=%s run=%s",
                    getattr(output_envelope.org, "id", ""),
                    run.org_id,
                )
                return Response(
                    {"error": "Org mismatch in output envelope"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Map ValidationStatus to ValidationRunStatus
            status_mapping = {
                ValidationStatus.SUCCESS: ValidationRunStatus.SUCCEEDED,
                ValidationStatus.FAILED_VALIDATION: ValidationRunStatus.FAILED,
                ValidationStatus.FAILED_RUNTIME: ValidationRunStatus.FAILED,
                ValidationStatus.CANCELLED: ValidationRunStatus.CANCELED,
            }
            step_status_mapping = {
                ValidationStatus.SUCCESS: StepStatus.PASSED,
                ValidationStatus.FAILED_VALIDATION: StepStatus.FAILED,
                ValidationStatus.FAILED_RUNTIME: StepStatus.FAILED,
                ValidationStatus.CANCELLED: StepStatus.SKIPPED,
            }

            # Update ValidationRun with results
            run.status = status_mapping.get(
                output_envelope.status,
                ValidationRunStatus.FAILED,
            )

            # Set timestamps
            finished_at = _coerce_finished_at(output_envelope.timing.finished_at)

            run.ended_at = finished_at

            # Calculate duration if we have both timestamps
            if run.started_at and run.ended_at:
                delta = run.ended_at - run.started_at
                run.duration_ms = int(delta.total_seconds() * 1000)

            # Store full envelope in summary field (for detailed analysis)
            run.summary = output_envelope.model_dump()

            # Extract error messages if validation failed
            if output_envelope.status != ValidationStatus.SUCCESS:
                error_messages = [
                    msg.text
                    for msg in output_envelope.messages
                    if msg.severity == "ERROR"
                ]
                if error_messages:
                    run.error = "\n".join(error_messages)
            else:
                run.error = ""

            run.save()

            # Update the step run with detailed status/timing/output
            step_run.status = step_status_mapping.get(
                output_envelope.status,
                StepStatus.FAILED,
            )
            step_run.ended_at = finished_at
            if not step_run.started_at:
                step_run.started_at = finished_at
            if step_run.started_at and step_run.ended_at:
                step_run.duration_ms = max(
                    int(
                        (step_run.ended_at - step_run.started_at).total_seconds()
                        * 1000,
                    ),
                    0,
                )
            step_run.output = output_envelope.model_dump()
            if output_envelope.status != ValidationStatus.SUCCESS:
                step_run.error = run.error or ""
            step_run.save(
                update_fields=[
                    "status",
                    "ended_at",
                    "started_at",
                    "duration_ms",
                    "output",
                    "error",
                ],
            )

            # Replace existing findings for this step run with envelope messages
            ValidationFinding.objects.filter(validation_step_run=step_run).delete()
            findings_to_create: list[ValidationFinding] = []
            for msg in output_envelope.messages:
                severity_value = (
                    msg.severity.value
                    if hasattr(msg.severity, "value")
                    else str(msg.severity)
                )
                location = getattr(msg, "location", None)
                path = getattr(location, "path", None) or ""
                meta: dict = {}
                if location:
                    meta.update(
                        {
                            "line": getattr(location, "line", None),
                            "column": getattr(location, "column", None),
                        }
                    )
                if msg.tags:
                    meta["tags"] = msg.tags
                finding = ValidationFinding(
                    validation_run=run,
                    validation_step_run=step_run,
                    severity=severity_value,
                    code=msg.code or "",
                    message=msg.text,
                    path=path,
                    meta=meta,
                )
                try:
                    finding._ensure_run_alignment()  # noqa: SLF001
                    finding._strip_payload_prefix()  # noqa: SLF001
                except Exception:
                    logger.warning(
                        "Skipping finding due to cleanup failure",
                        exc_info=True,
                    )
                    continue
                findings_to_create.append(finding)

            if findings_to_create:
                try:
                    ValidationFinding.objects.bulk_create(
                        findings_to_create,
                        batch_size=500,
                    )
                except Exception:
                    logger.exception(
                        "Failed to persist findings for step_run %s; continuing",
                        step_run.id,
                    )

            # Rebuild summaries based on stored findings
            severity_counts_run: Counter[str] = Counter()
            for row in (
                ValidationFinding.objects.filter(validation_run=run)
                .values("severity")
                .annotate(count=Count("id"))
            ):
                severity_counts_run[row["severity"]] = row["count"]

            severity_counts_step: Counter[str] = Counter()
            for row in (
                ValidationFinding.objects.filter(validation_step_run=step_run)
                .values("severity")
                .annotate(count=Count("id"))
            ):
                severity_counts_step[row["severity"]] = row["count"]

            total_findings_run = sum(severity_counts_run.values())

            run_summary, _ = ValidationRunSummary.objects.update_or_create(
                run=run,
                defaults={
                    "status": run.status,
                    "completed_at": run.ended_at,
                    "total_findings": total_findings_run,
                    "error_count": severity_counts_run.get(Severity.ERROR.value, 0),
                    "warning_count": severity_counts_run.get(Severity.WARNING.value, 0),
                    "info_count": severity_counts_run.get(Severity.INFO.value, 0),
                    "assertion_failure_count": 0,
                    "assertion_total_count": 0,
                    "extras": {},
                },
            )

            ValidationStepRunSummary.objects.update_or_create(
                step_run=step_run,
                defaults={
                    "summary": run_summary,
                    "step_name": getattr(step_run.workflow_step, "name", ""),
                    "step_order": step_run.step_order or 0,
                    "status": step_run.status,
                    "error_count": severity_counts_step.get(
                        Severity.ERROR.value,
                        0,
                    ),
                    "warning_count": severity_counts_step.get(
                        Severity.WARNING.value,
                        0,
                    ),
                    "info_count": severity_counts_step.get(
                        Severity.INFO.value,
                        0,
                    ),
                    "assertion_failure_count": 0,
                    "assertion_total_count": 0,
                },
            )

            logger.info(
                "Successfully processed callback for run %s",
                callback.run_id,
            )

            return Response(
                {"message": "Callback processed successfully"},
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            logger.exception("Unexpected error processing callback")
            return Response(
                {"error": f"Internal server error: {e}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
