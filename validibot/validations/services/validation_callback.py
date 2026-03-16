"""
Service layer for processing validator callbacks on the worker service.

Validator containers (EnergyPlus, FMU) POST a minimal callback payload to the
worker-only callback endpoint when they complete. The callback handler:

1. Validates the payload shape (Pydantic model from validibot_shared)
2. Downloads the full output envelope from cloud storage
3. Delegates to ValidationStepProcessor.complete_from_callback() for step completion
4. Either dispatches a resume task (when more steps remain) or finalizes the run

The public API view should be a thin wrapper around this service.

NOTE: Assertion evaluation and finding persistence are handled by the processor,
not by this service. The processor calls validator.post_execute_validate() which
handles all assertion types (CEL, etc.) for the output stage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any

from django.db import DatabaseError
from django.db import transaction
from pydantic import ValidationError
from rest_framework import status
from rest_framework.response import Response
from validibot_shared.validations.envelopes import ValidationCallback
from validibot_shared.validations.envelopes import ValidationStatus

from validibot.core.models import CallbackReceiptStatus
from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunErrorCategory
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import CallbackReceipt
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationStepRun
from validibot.validations.services.cloud_run.gcs_client import download_envelope
from validibot.validations.services.validation_run import ValidationRunService

logger = logging.getLogger(__name__)


# ── Exception for early-exit error responses ──────────────────────────
#
# _process_callback delegates to several helper methods that each validate
# preconditions (active step run exists, envelope downloads successfully,
# envelope IDs match the expected run/validator).  Rather than threading
# Response objects back through return values, helpers raise this exception
# and the top-level method catches it once and converts it to a Response.


class _CallbackProcessingError(Exception):
    """Raised by helpers when callback processing cannot continue.

    Carries the HTTP status code and response body so _process_callback
    can convert it to a DRF Response in one place.
    """

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# ── Result container for step completion ──────────────────────────────


@dataclass(frozen=True)
class _StepCompletionResult:
    """Intermediate result from _complete_step, consumed by _finalize_or_resume."""

    step_run: ValidationStepRun
    step_status: StepStatus
    step_error: str
    output_envelope: Any  # typed as Any to avoid coupling to envelope base class


# ── Helpers ───────────────────────────────────────────────────────────


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
    processor handles all of that via validator.post_execute_validate().
    """

    # ── Public entry point ────────────────────────────────────────────

    def process(self, *, payload: dict) -> Response:
        """
        Validate and process a validator callback payload.

        Args:
            payload: Incoming request body (JSON) containing callback data.

        Returns:
            DRF Response with an appropriate status code and body.
        """
        try:
            # The callback is intentionally minimal — it just says "run X
            # finished with status Y, go fetch the full results from this URI."
            # The actual validation-specific data (findings, outputs) lives in
            # the output.json at result_uri, which Django downloads and
            # processes separately. This keeps the callback contract stable
            # across all validator types — the container doesn't need to
            # serialize its full output into the HTTP POST.
            callback = ValidationCallback.model_validate(payload)

            logger.info(
                "Received callback for run %s with status %s (callback_id=%s)",
                callback.run_id,
                callback.status,
                callback.callback_id,
            )

            # Get the validation run FIRST — before idempotency check.
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

            return self._process_with_idempotency_guard(callback, run)

        except ValidationError as exc:
            logger.warning("Invalid callback payload: %s", exc)
            return Response(
                {"error": f"Invalid callback payload: {exc}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as exc:
            logger.exception("Unexpected error processing callback")
            return Response(
                {"error": f"Internal server error: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    # ── Idempotency guard ─────────────────────────────────────────────

    def _process_with_idempotency_guard(
        self,
        callback: ValidationCallback,
        run: ValidationRun,
    ) -> Response:
        """
        Ensure exactly-once processing for callbacks with a callback_id.

        Cloud Tasks (and most message queues) guarantee at-least-once delivery,
        so duplicate callbacks are expected. This method uses a CallbackReceipt
        table as an idempotency ledger:

        1. No callback_id → skip idempotency, process immediately.
        2. New callback_id → create a PROCESSING receipt, process under a
           row-level lock so no concurrent request can duplicate the work.
        3. Existing receipt still PROCESSING → previous attempt crashed
           mid-flight, so retry.
        4. Existing receipt in a terminal state → true duplicate, return
           the cached "already processed" response.
        5. Lock contention (another request holds the row lock) → return
           409 so Cloud Tasks retries later.
        """
        if not callback.callback_id:
            return self._process_callback(
                callback=callback,
                run=run,
                receipt=None,
            )

        try:
            with transaction.atomic():
                receipt, receipt_created = self._get_or_create_receipt(
                    callback,
                    run,
                )

                if not receipt_created:
                    if receipt.status != CallbackReceiptStatus.PROCESSING:
                        logger.info(
                            "Callback %s already processed at %s",
                            callback.callback_id,
                            receipt.received_at,
                        )
                        # Return inside the atomic block isn't needed for
                        # processing, but exiting cleanly releases the lock.
                        return Response(
                            {
                                "message": "Callback already processed",
                                "idempotent_replayed": True,
                                "original_received_at": (
                                    receipt.received_at.isoformat()
                                ),
                            },
                            status=status.HTTP_200_OK,
                        )

                    logger.info(
                        "Callback %s still PROCESSING (previous attempt "
                        "failed), retrying",
                        callback.callback_id,
                    )

                # Process inside the transaction so the row lock is held
                # until the receipt status reaches a terminal value.
                return self._process_callback(
                    callback=callback,
                    run=run,
                    receipt=receipt,
                )

        except DatabaseError:
            # Lock acquisition failed — another request is processing this
            # callback. Return 409 so Cloud Tasks retries later.
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

    @staticmethod
    def _get_or_create_receipt(
        callback: ValidationCallback,
        run: ValidationRun,
    ) -> tuple[CallbackReceipt, bool]:
        """
        Fetch an existing receipt under a row lock, or create a new one.

        Returns:
            (receipt, created) — mirrors Django's get_or_create convention.
        """
        try:
            receipt = CallbackReceipt.objects.select_for_update(
                nowait=True,
            ).get(callback_id=callback.callback_id)
        except CallbackReceipt.DoesNotExist:
            receipt = CallbackReceipt.objects.create(
                callback_id=callback.callback_id,
                validation_run=run,
                status=CallbackReceiptStatus.PROCESSING,
                result_uri=callback.result_uri or "",
            )
            return receipt, True
        else:
            return receipt, False

    # ── Core processing pipeline ──────────────────────────────────────

    def _process_callback(
        self,
        *,
        callback: ValidationCallback,
        run: ValidationRun,
        receipt: CallbackReceipt | None,
    ) -> Response:
        """
        Orchestrate the callback processing pipeline.

        This method is the core of the service, called either inside a
        transaction (with idempotency guard) or directly (without). It
        delegates to focused helper methods for each phase:

        1. Resolve the active step run and its validator
        2. Download and validate the output envelope from GCS
        3. Complete the step (processor handles findings + assertions)
        4. Either resume the next step or finalize the run
        5. Mark the receipt as completed (idempotency bookkeeping)
        """
        try:
            step_run, validator = self._resolve_active_step_run(run)
            output_envelope = self._download_and_validate_envelope(
                callback,
                run,
                validator,
            )
            result = self._complete_step(run, step_run, output_envelope)
            self._finalize_or_resume(run, result)
        except _CallbackProcessingError as exc:
            return Response(
                {"error": exc.detail},
                status=exc.status_code,
            )

        self._mark_receipt_completed(callback, receipt, run)

        logger.info(
            "Processed callback for run %s, step status=%s",
            callback.run_id,
            result.step_status,
        )

        return Response(
            {"message": "Callback processed successfully"},
            status=status.HTTP_200_OK,
        )

    # ── Step 1: Resolve the active step run ───────────────────────────

    @staticmethod
    def _resolve_active_step_run(
        run: ValidationRun,
    ) -> tuple[ValidationStepRun, Any]:
        """
        Find the active (RUNNING/PENDING) step run and its validator.

        We use select_related rather than run.current_step_run because we
        need step_run.workflow_step.validator in subsequent steps.

        Returns:
            (step_run, validator) tuple.

        Raises:
            _CallbackProcessingError: If no active step run or no validator.
        """
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
            raise _CallbackProcessingError(
                status.HTTP_404_NOT_FOUND,
                "Step run not found",
            )

        validator = step_run.workflow_step.validator
        if not validator:
            logger.error("No validator found for step run: %s", step_run.id)
            raise _CallbackProcessingError(
                status.HTTP_400_BAD_REQUEST,
                "No validator found for step",
            )

        return step_run, validator

    # ── Step 2: Download and validate the output envelope ─────────────

    @staticmethod
    def _download_and_validate_envelope(
        callback: ValidationCallback,
        run: ValidationRun,
        validator,
    ):
        """
        Download the output envelope from GCS and verify it matches expectations.

        Each advanced validator declares its output envelope class in its
        ValidatorConfig; these are resolved at startup and stored in the
        validator registry for O(1) lookups.

        Raises:
            _CallbackProcessingError: On download failure, missing envelope
                class, or validator/run ID mismatch.
        """
        from validibot.validations.validators.base import registry

        envelope_class = registry.get_output_envelope_class(
            validator.validation_type,
        )
        if not envelope_class:
            logger.error(
                "No output envelope class registered for validator type: %s",
                validator.validation_type,
            )
            raise _CallbackProcessingError(
                status.HTTP_400_BAD_REQUEST,
                f"No output envelope class registered for "
                f"validator type: {validator.validation_type}",
            )

        try:
            output_envelope = download_envelope(
                callback.result_uri,
                envelope_class,
            )
        except Exception as exc:
            logger.exception("Failed to download output envelope")
            raise _CallbackProcessingError(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"Failed to download output envelope: {exc}",
            ) from exc

        # Verify the envelope belongs to the expected validator and run.
        # We don't validate org/workflow because ValidationOutputEnvelope
        # doesn't contain those fields — they're only in the input envelope.
        # The run_id check is sufficient to match callback to run.
        if str(output_envelope.validator.id) != str(validator.id):
            logger.warning(
                "Envelope validator mismatch: envelope=%s expected=%s",
                output_envelope.validator.id,
                validator.id,
            )
            raise _CallbackProcessingError(
                status.HTTP_400_BAD_REQUEST,
                "Validator mismatch in output envelope",
            )

        if str(getattr(output_envelope, "run_id", "")) != str(run.id):
            logger.warning(
                "Envelope run mismatch: envelope=%s expected=%s",
                getattr(output_envelope, "run_id", ""),
                run.id,
            )
            raise _CallbackProcessingError(
                status.HTTP_400_BAD_REQUEST,
                "Run mismatch in output envelope",
            )

        return output_envelope

    # ── Step 3: Complete the step via the processor ───────────────────

    def _complete_step(
        self,
        run: ValidationRun,
        step_run: ValidationStepRun,
        output_envelope,
    ) -> _StepCompletionResult:
        """
        Delegate to ValidationStepProcessor and emit the step-completed signal.

        The processor handles findings persistence, output-stage assertion
        evaluation, and signal storage. After it completes, we refresh the
        step_run from the database to get the authoritative final status
        (which may differ from the envelope status if assertions failed).

        Returns:
            _StepCompletionResult with the refreshed step status and error.
        """
        from validibot.validations.services.step_processor import get_step_processor

        processor = get_step_processor(run, step_run)
        processor.complete_from_callback(output_envelope)

        # Refresh step_run to get the final status set by the processor.
        # The processor's finalize_step() sets step_run.status based on:
        # 1. Container envelope status (authoritative for container execution)
        # 2. Output-stage assertion failures (can fail step even if container
        #    succeeded)
        # We must use this status, NOT output_envelope.status, for run-level
        # decisions.
        step_run.refresh_from_db()

        # Notify listeners that a step completed (e.g., cloud metering for
        # credit deduction). Using send_robust so a failing receiver doesn't
        # break the callback flow.
        from validibot.validations.signals import validation_step_completed

        validation_step_completed.send_robust(
            sender=self.__class__,
            step_run=step_run,
            validation_run=run,
        )

        return _StepCompletionResult(
            step_run=step_run,
            step_status=StepStatus(step_run.status),
            step_error=step_run.error or "",
            output_envelope=output_envelope,
        )

    # ── Step 4: Resume next step or finalize the run ──────────────────

    def _finalize_or_resume(
        self,
        run: ValidationRun,
        result: _StepCompletionResult,
    ) -> None:
        """
        Either enqueue the next workflow step or finalize the run.

        If more steps remain and the current step passed, enqueue a resume
        task for the next step. Otherwise, finalize the run with the
        appropriate status, error category, timing, summary, and evidence hash.
        """
        remaining_steps = run.workflow.steps.filter(
            order__gt=result.step_run.step_order,
        ).exists()

        if remaining_steps and result.step_status == StepStatus.PASSED:
            self._enqueue_next_step(run, result.step_run)
        else:
            self._finalize_run(run, result)

    @staticmethod
    def _enqueue_next_step(
        run: ValidationRun,
        step_run: ValidationStepRun,
    ) -> None:
        """Enqueue a Cloud Task to resume the workflow after the completed step.

        Passes the completed step's order so the orchestrator can filter with
        ``order__gt`` to find the next step. This avoids the fragile ``+ 1``
        assumption — WorkflowStep.order uses gapped numbering (10, 20, 30…)
        so ``step_order + 1`` would produce a value that doesn't correspond
        to any real step.
        """
        from validibot.core.tasks import enqueue_validation_run

        # user_id can be NULL if the run was created via API without user
        # context. Pass 0 to signal "no user" — execute_workflow_steps()
        # handles this gracefully.
        enqueue_validation_run(
            validation_run_id=run.id,
            user_id=run.user_id or 0,
            resume_from_step=step_run.step_order,
        )
        logger.info(
            "Enqueued resume task for run %s after step_order %s",
            run.id,
            step_run.step_order,
        )

    def _finalize_run(
        self,
        run: ValidationRun,
        result: _StepCompletionResult,
    ) -> None:
        """
        Finalize the validation run after the last step (or a failed step).

        Sets run status, error category, timing, rebuilds the summary record,
        stamps the evidence hash, and queues a submission purge if the
        retention policy requires it.
        """
        # Map step status to run status. We use the processor's step_status
        # (not envelope status) because the processor accounts for output-stage
        # assertion failures that can fail a step even when the container
        # returned SUCCESS.
        step_to_run_status = {
            StepStatus.PASSED: ValidationRunStatus.SUCCEEDED,
            StepStatus.FAILED: ValidationRunStatus.FAILED,
            StepStatus.SKIPPED: ValidationRunStatus.CANCELED,
        }

        # Determine error category based on envelope status (runtime vs
        # validation) combined with step status (for assertion failures).
        if result.step_status in {StepStatus.PASSED, StepStatus.SKIPPED}:
            error_category = ""
        elif result.output_envelope.status == ValidationStatus.FAILED_RUNTIME:
            error_category = ValidationRunErrorCategory.RUNTIME_ERROR
        else:
            # FAILED_VALIDATION, SUCCESS with assertion failures, or unknown
            error_category = ValidationRunErrorCategory.VALIDATION_FAILED

        finished_at = _coerce_finished_at(
            result.output_envelope.timing.finished_at,
        )

        run.status = step_to_run_status.get(
            result.step_status,
            ValidationRunStatus.FAILED,
        )
        run.error_category = error_category
        run.ended_at = finished_at
        run.error = result.step_error

        if run.started_at and run.ended_at:
            delta = run.ended_at - run.started_at
            run.duration_ms = int(delta.total_seconds() * 1000)

        run.save()

        ValidationRunService().rebuild_run_summary_record(
            validation_run=run,
        )

        from validibot.validations.services.evidence_hash import (
            safe_stamp_evidence_hash,
        )

        safe_stamp_evidence_hash(run)

        logger.info(
            "Finalized run %s with status %s",
            run.id,
            run.status,
        )
        self._queue_purge_if_do_not_store(run)

    # ── Step 5: Receipt bookkeeping ───────────────────────────────────

    @staticmethod
    def _mark_receipt_completed(
        callback: ValidationCallback,
        receipt: CallbackReceipt | None,
        run: ValidationRun,
    ) -> None:
        """
        Update receipt status from PROCESSING to COMPLETED.

        The receipt tracks callback processing state, not step outcome.
        A failure here is logged but does not fail the request — the run
        is already in a consistent state.
        """
        if not callback.callback_id or not receipt:
            return

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
            logger.warning(
                "Failed to update callback receipt for %s",
                callback.callback_id,
                exc_info=True,
            )

    # ── Submission purge ──────────────────────────────────────────────

    @staticmethod
    def _queue_purge_if_do_not_store(run: ValidationRun) -> None:
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
