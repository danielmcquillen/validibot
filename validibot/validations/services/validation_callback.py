"""
Service layer for processing validator callbacks on the worker service.

Validator containers (EnergyPlus, FMI) POST a minimal callback payload to the
worker-only callback endpoint when they complete. The callback handler:

1. Validates the payload shape (Pydantic model from vb_shared)
2. Downloads the full output envelope from cloud storage
3. Delegates to ValidationStepProcessor.complete_from_callback() for step completion
4. Either dispatches a resume task (when more steps remain) or finalizes the run

The public API view should be a thin wrapper around this service.

NOTE: Assertion evaluation and finding persistence are handled by the processor,
not by this service. The processor calls engine.post_execute_validate() which
handles all assertion types (CEL, etc.) for the output stage.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime

from django.db import DatabaseError
from django.db import transaction
from rest_framework import status
from rest_framework.response import Response
from vb_shared.energyplus.envelopes import EnergyPlusOutputEnvelope
from vb_shared.fmi.envelopes import FMIOutputEnvelope
from vb_shared.validations.envelopes import ValidationCallback
from vb_shared.validations.envelopes import ValidationStatus

from validibot.core.models import CallbackReceiptStatus
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunErrorCategory
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.models import CallbackReceipt
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationStepRun
from validibot.validations.services.cloud_run.gcs_client import download_envelope
from validibot.validations.services.validation_run import ValidationRunService

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
        return dt_value.replace(tzinfo=UTC)
    return dt_value.astimezone(UTC)


class ValidationCallbackService:
    """
    Process container-based validator callbacks.

    This service is invoked by the worker-only callback API endpoint. It handles
    idempotency, envelope download, and run finalization.

    IMPORTANT: This class is only used for async backends (GCP Cloud Run, AWS
    Fargate) where containers POST callbacks when complete. For sync backends
    (Docker Compose), the processor handles completion inline.

    Responsibilities:
    - Validate callback payload and check idempotency
    - Download output envelope from cloud storage
    - Delegate to ValidationStepProcessor.complete_from_callback()
    - Enqueue resume task (more steps) or finalize run (last step)

    Finding persistence and assertion evaluation are NOT done here - the
    processor handles all of that via engine.post_execute_validate().
    """

    def process(self, *, payload: dict) -> Response:
        """
        Validate and process a validator callback payload.

        Args:
            payload: Incoming request body (JSON) containing callback data.

        Returns:
            DRF Response with an appropriate status code and body.
        """
        try:
            callback = ValidationCallback.model_validate(payload)

            logger.info(
                "Received callback for run %s with status %s (callback_id=%s)",
                callback.run_id,
                callback.status,
                callback.callback_id,
            )

            # Get the validation run FIRST - before idempotency check.
            # This ensures we return a clean 404 if the run doesn't exist,
            # rather than an FK error when creating the receipt.
            try:
                run = ValidationRun.objects.get(id=callback.run_id)
            except ValidationRun.DoesNotExist:
                logger.warning("Validation run not found: %s", callback.run_id)
                return Response(
                    {"error": "Validation run not found"},
                    status=status.HTTP_404_NOT_FOUND,
                )

            receipt = None
            receipt_created = False
            should_process = True
            if callback.callback_id:
                try:
                    with transaction.atomic():
                        # Try to get existing receipt with lock
                        try:
                            receipt = CallbackReceipt.objects.select_for_update(
                                nowait=True
                            ).get(callback_id=callback.callback_id)
                            receipt_created = False
                        except CallbackReceipt.DoesNotExist:
                            # No receipt exists - create one with PROCESSING status
                            receipt = CallbackReceipt.objects.create(
                                callback_id=callback.callback_id,
                                validation_run=run,
                                status=CallbackReceiptStatus.PROCESSING,
                                result_uri=callback.result_uri or "",
                            )
                            receipt_created = True

                        if not receipt_created:
                                # Receipt already exists - check if it was fully processed.
                                # If status is still PROCESSING, a previous attempt failed
                                # mid-processing, so we should retry. Otherwise, it's a true
                                # duplicate and we return the cached response.
                                if receipt.status != CallbackReceiptStatus.PROCESSING:
                                    logger.info(
                                        "Callback %s already processed at %s",
                                        callback.callback_id,
                                        receipt.received_at,
                                    )
                                should_process = False
                            else:
                                # Status is PROCESSING - previous attempt failed, retry
                                logger.info(
                                    "Callback %s still PROCESSING (previous attempt "
                                    "failed), retrying",
                                    callback.callback_id,
                                )

                        # Process the callback inside the transaction so the lock is
                        # held until we update the receipt status to a terminal value.
                        if should_process:
                            return self._process_callback(
                                callback=callback,
                                run=run,
                                receipt=receipt,
                            )

                except DatabaseError:
                    # Lock acquisition failed - another callback is processing
                    # Return 409 Conflict so Cloud Tasks will retry later
                    logger.info(
                        "Callback %s locked by concurrent request, returning 409",
                        callback.callback_id,
                    )
                    return Response(
                        {
                            "message": "Callback is being processed by another request",
                            "retry": True,
                        },
                        status=status.HTTP_409_CONFLICT,
                    )

                # If we get here, it means should_process was False (duplicate callback)
                if not should_process and receipt:
                    received_at_iso = receipt.received_at.isoformat()
                    return Response(
                        {
                            "message": "Callback already processed",
                            "idempotent_replayed": True,
                            "original_received_at": received_at_iso,
                        },
                        status=status.HTTP_200_OK,
                    )

            # No callback_id - process without idempotency protection
            return self._process_callback(
                callback=callback,
                run=run,
                receipt=None,
            )

        except Exception as exc:
            logger.exception("Unexpected error processing callback")
            return Response(
                {"error": f"Internal server error: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    def _process_callback(
        self,
        *,
        callback: ValidationCallback,
        run: ValidationRun,
        receipt: CallbackReceipt | None,
    ) -> Response:
        """
        Process the callback payload and update the validation run.

        This method contains the core callback processing logic, extracted to
        allow it to run inside a transaction that holds a row lock on the
        callback receipt (for idempotency).

        Args:
            callback: The validated callback payload.
            run: The ValidationRun being updated.
            receipt: Optional CallbackReceipt for idempotency tracking.

        Returns:
            DRF Response to send back to the caller.
        """
        # Locate the active step run (RUNNING/PENDING) for this validation.
        step_run = run.current_step_run
        if not step_run:
            step_run = (
                ValidationStepRun.objects.select_related(
                    "workflow_step__validator",
                )
                .filter(
                    validation_run=run,
                    status__in=[StepStatus.RUNNING, StepStatus.PENDING],
                )
                .order_by("step_order")
                .first()
            )

        if not step_run:
            logger.warning("No active step run found for run %s", run.id)
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

        # Determine the envelope class based on validator type
        if validator.validation_type == ValidationType.ENERGYPLUS:
            envelope_class = EnergyPlusOutputEnvelope
        elif validator.validation_type == ValidationType.FMI:
            envelope_class = FMIOutputEnvelope
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
        except Exception as exc:
            logger.exception("Failed to download output envelope")
            return Response(
                {"error": f"Failed to download output envelope: {exc}"},
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

        if str(getattr(output_envelope, "run_id", "")) != str(run.id):
            logger.warning(
                "Envelope run mismatch: envelope=%s expected=%s",
                getattr(output_envelope, "run_id", ""),
                run.id,
            )
            return Response(
                {"error": "Run mismatch in output envelope"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Note: We don't validate org/workflow from the output envelope because
        # ValidationOutputEnvelope doesn't contain those fields - they're only in
        # the input envelope. The run_id check above is sufficient to match the
        # callback to the correct run.

        # Use the processor to complete the step from the callback
        # This handles: findings persistence, output-stage assertions, signals storage
        from validibot.validations.services.step_processor import get_step_processor

        processor = get_step_processor(run, step_run)
        processor.complete_from_callback(output_envelope)

        # Refresh step_run to get the final status set by the processor.
        # The processor's finalize_step() sets step_run.status based on:
        # 1. Container envelope status (authoritative for container execution)
        # 2. Output-stage assertion failures (can fail step even if container succeeded)
        # We must use this status, NOT output_envelope.status, for run-level decisions.
        step_run.refresh_from_db()
        step_status = StepStatus(step_run.status)

        # Extract error for run-level error message from step_run
        # (processor already extracted and stored this)
        step_error = step_run.error or ""

        # NOTE: The processor has already:
        # 1. Persisted findings (envelope messages + assertion results)
        # 2. Evaluated output-stage assertions
        # 3. Stored signals in run.summary["steps"][step_id]["signals"]
        # 4. Finalized step_run with status, timing, output

        # Get finished_at for run-level finalization
        finished_at = _coerce_finished_at(output_envelope.timing.finished_at)

        # Only proceed with remaining steps if the current step passed
        remaining_steps = run.workflow.steps.filter(
            order__gt=step_run.step_order,
        ).exists()
        if remaining_steps and step_status == StepStatus.PASSED:
            from validibot.core.tasks import enqueue_validation_run

            # user_id can be NULL if the run was created via API without user context.
            # Pass 0 to signal "no user" - execute_workflow_steps() handles this
            # gracefully.
            enqueue_validation_run(
                validation_run_id=run.id,
                user_id=run.user_id or 0,
                resume_from_step=step_run.step_order + 1,
            )
            logger.info(
                "Enqueued resume task for run %s from step %s",
                run.id,
                step_run.step_order + 1,
            )
        else:
            # Map step status to run status.
            # We use step_status (from processor) rather than envelope status because
            # the processor accounts for output-stage assertion failures that can fail
            # the step even when the container returned SUCCESS.
            step_to_run_status = {
                StepStatus.PASSED: ValidationRunStatus.SUCCEEDED,
                StepStatus.FAILED: ValidationRunStatus.FAILED,
                StepStatus.SKIPPED: ValidationRunStatus.CANCELED,
            }

            # Determine error category based on envelope status (for runtime vs
            # validation) combined with step status (for assertion failures)
            if step_status == StepStatus.PASSED:
                error_category = ""
            elif output_envelope.status == ValidationStatus.FAILED_RUNTIME:
                error_category = ValidationRunErrorCategory.RUNTIME_ERROR
            else:
                # FAILED_VALIDATION, SUCCESS with assertion failures, or unknown
                error_category = ValidationRunErrorCategory.VALIDATION_FAILED

            run.status = step_to_run_status.get(
                step_status,
                ValidationRunStatus.FAILED,
            )
            run.error_category = error_category
            run.ended_at = finished_at
            run.error = step_error

            if run.started_at and run.ended_at:
                delta = run.ended_at - run.started_at
                run.duration_ms = int(delta.total_seconds() * 1000)

            run.save()

            ValidationRunService().rebuild_run_summary_record(
                validation_run=run,
            )

            logger.info(
                "Finalized run %s with status %s",
                run.id,
                run.status,
            )
            self._queue_purge_if_do_not_store(run)

        # Update the receipt status from PROCESSING to COMPLETED.
        # The receipt tracks callback processing, not step outcome.
        if callback.callback_id and receipt:
            try:
                receipt.status = CallbackReceiptStatus.COMPLETED
                receipt.validation_run = run
                receipt.save(update_fields=["status", "validation_run"])
                logger.debug(
                    "Updated callback receipt %s to status %s",
                    callback.callback_id,
                    receipt.status,
                )
            except Exception:
                # If receipt update fails, log but don't fail the request.
                logger.warning(
                    "Failed to update callback receipt for %s",
                    callback.callback_id,
                    exc_info=True,
                )

        logger.info(
            "Processed callback for run %s, step status=%s",
            callback.run_id,
            step_status,
        )

        return Response(
            {"message": "Callback processed successfully"},
            status=status.HTTP_200_OK,
        )

    def _queue_purge_if_do_not_store(self, run: ValidationRun) -> None:
        """
        Queue submission purge if the retention policy is DO_NOT_STORE.

        Validator callbacks should be fast and reliable. Instead of purging
        submission content inline (which may require deleting many GCS objects),
        we enqueue a purge record for the scheduled purge worker to process.

        Args:
            run: The ValidationRun that just completed.
        """
        from validibot.submissions.constants import DataRetention
        from validibot.submissions.models import queue_submission_purge

        submission = run.submission
        if not submission:
            return

        if submission.retention_policy != DataRetention.DO_NOT_STORE:
            return

        if submission.content_purged_at:
            return

        try:
            queue_submission_purge(submission)
            logger.info(
                "Queued DO_NOT_STORE submission purge after run completion",
                extra={
                    "submission_id": str(submission.id),
                    "run_id": str(run.id),
                },
            )
        except Exception:
            logger.exception(
                "Failed to queue DO_NOT_STORE submission purge",
                extra={
                    "submission_id": str(submission.id),
                    "run_id": str(run.id),
                },
            )
