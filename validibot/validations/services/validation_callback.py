"""
Service layer for processing validator callbacks on the worker service.

Validator containers (EnergyPlus, FMI) POST a minimal callback payload to the
worker-only callback endpoint when they complete. The callback handler:

1. Validates the payload shape (Pydantic model from vb_shared)
2. Downloads the full output envelope from GCS
3. Persists step/run status, findings, and signals for downstream steps
4. Either enqueues a resume task (when more steps remain) or finalizes the run

The public API view should be a thin wrapper around this service.
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
from validibot.validations.models import ValidationFinding
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationStepRun
from validibot.validations.services.cloud_run.gcs_client import download_envelope
from validibot.validations.services.validation_run import ValidationRunService

logger = logging.getLogger(__name__)


def _extract_output_payload_from_envelope(output_envelope) -> dict | None:
    """
    Extract the output payload from an envelope for CEL assertion evaluation.

    Different envelope types store outputs in different structures. This function
    normalizes them to a dict suitable for CEL evaluation.

    Args:
        output_envelope: The EnergyPlus or FMI output envelope.

    Returns:
        A dict of output signals keyed by catalog slug, or None if not available.
    """
    try:
        # FMI envelopes expose output_values keyed by catalog slug
        signals = getattr(output_envelope.outputs, "output_values", None)
        if signals is None and hasattr(output_envelope.outputs, "outputs"):
            signals = getattr(output_envelope.outputs, "outputs", None)
    except Exception:
        logger.debug("Could not extract output payload from envelope")
        return None

    if not signals:
        return None
    # Serialize Pydantic models to dicts for CEL evaluation
    if hasattr(signals, "model_dump"):
        return signals.model_dump(mode="json")
    if isinstance(signals, dict):
        return signals
    return None


def _evaluate_output_stage_assertions(
    *,
    step_run: ValidationStepRun,
    output_payload: dict,
) -> list[ValidationFinding]:
    """
    Evaluate output-stage CEL assertions after an async validator completes.

    After a Cloud Run Job (EnergyPlus, FMI) completes and returns outputs, this
    function evaluates any ruleset assertions that target output-stage catalog
    entries. This includes generating success messages for passed assertions.

    Args:
        step_run: The ValidationStepRun being finalized.
        output_payload: Dict of output signals keyed by catalog slug.

    Returns:
        List of ValidationFinding objects for assertion results (failures and
        success messages).
    """
    from validibot.actions.protocols import RunContext
    from validibot.validations.engines.registry import get as get_validator_class

    workflow_step = step_run.workflow_step
    if not workflow_step:
        return []

    validator = workflow_step.validator
    ruleset = workflow_step.ruleset
    if not validator or not ruleset:
        return []

    # Check if ruleset has any CEL assertions
    if not ruleset.assertions.filter(assertion_type="cel_expr").exists():
        return []

    # Get the appropriate engine class for this validator type
    try:
        engine_cls = get_validator_class(validator.validation_type)
    except Exception:
        logger.warning(
            "Could not get validator engine class for type %s",
            validator.validation_type,
        )
        return []

    # Create engine instance and set up run_context for success message support
    engine = engine_cls()
    engine.run_context = RunContext(step=workflow_step)

    # Evaluate output-stage assertions
    try:
        issues = engine.evaluate_cel_assertions(
            ruleset=ruleset,
            validator=validator,
            payload=output_payload,
            target_stage="output",
        )
    except Exception:
        logger.exception(
            "Failed to evaluate output-stage assertions for step_run %s",
            step_run.id,
        )
        return []

    # Convert ValidationIssue objects to ValidationFinding records
    findings: list[ValidationFinding] = []
    for issue in issues:
        severity_value = (
            issue.severity.value
            if hasattr(issue.severity, "value")
            else str(issue.severity)
        )
        finding = ValidationFinding(
            validation_run=step_run.validation_run,
            validation_step_run=step_run,
            severity=severity_value,
            code=issue.code or "",
            message=issue.message or "",
            path=issue.path or "",
            meta=issue.meta or {},
            ruleset_assertion_id=issue.assertion_id,
        )
        try:
            finding._ensure_run_alignment()  # noqa: SLF001
        except Exception:
            logger.warning(
                "Skipping assertion finding due to alignment failure",
                exc_info=True,
            )
            continue
        findings.append(finding)

    return findings


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
    Process Cloud Run Job validator callbacks.

    This service is invoked by the worker-only callback API endpoint. It contains
    the orchestration logic for idempotency, envelope download, persistence, and
    resuming/finalizing validation runs.
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
                            receipt = (
                                CallbackReceipt.objects.select_for_update(nowait=True)
                                .get(callback_id=callback.callback_id)
                            )
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

        # Map ValidationStatus to step status
        step_status_mapping = {
            ValidationStatus.SUCCESS: StepStatus.PASSED,
            ValidationStatus.FAILED_VALIDATION: StepStatus.FAILED,
            ValidationStatus.FAILED_RUNTIME: StepStatus.FAILED,
            ValidationStatus.CANCELLED: StepStatus.SKIPPED,
        }

        step_status = step_status_mapping.get(
            output_envelope.status,
            StepStatus.FAILED,
        )

        finished_at = _coerce_finished_at(output_envelope.timing.finished_at)

        # Extract error messages from envelope (for step-level errors)
        step_error = ""
        if output_envelope.status != ValidationStatus.SUCCESS:
            error_messages = [
                msg.text
                for msg in output_envelope.messages
                if msg.severity == "ERROR"
            ]
            if error_messages:
                step_error = "\n".join(error_messages)

        # Update the step run with detailed status/timing/output
        step_run.status = step_status
        step_run.ended_at = finished_at
        if not step_run.started_at:
            step_run.started_at = finished_at
        if step_run.started_at and step_run.ended_at:
            step_run.duration_ms = max(
                int(
                    (step_run.ended_at - step_run.started_at).total_seconds() * 1000,
                ),
                0,
            )

        # Persist full envelope plus a signals namespace for downstream steps.
        # Merge with any existing output (e.g., job metadata like execution_bundle_uri)
        # so the step retains launch-time diagnostics after callback finalization.
        # Use mode='json' to serialize datetime objects as ISO strings for JSONField.
        step_output = output_envelope.model_dump(mode="json")
        try:
            # FMI envelopes expose output_values keyed by catalog slug
            signals = getattr(output_envelope.outputs, "output_values", None)
            if signals is None and hasattr(output_envelope.outputs, "outputs"):
                signals = getattr(output_envelope.outputs, "outputs", None)
            if signals:
                # Serialize Pydantic models to dicts for JSONField storage
                if hasattr(signals, "model_dump"):
                    signals = signals.model_dump(mode="json")
                step_output = {**step_output, "signals": signals}
        except Exception:
            logger.exception(
                "Failed to extract signals from output envelope",
                extra={"run_id": run.id},
            )
        existing_output = dict(step_run.output or {})
        merged_output = {**existing_output, **step_output}

        # Defensive JSON serialization: ensure all values are JSON-safe before
        # saving to the database. This catches any Pydantic models or other
        # non-serializable objects that slipped through.
        import json

        def make_json_safe(obj):
            """Convert Pydantic models and other objects to JSON-safe types."""
            if hasattr(obj, "model_dump"):
                return obj.model_dump(mode="json")
            if isinstance(obj, dict):
                return {k: make_json_safe(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [make_json_safe(item) for item in obj]
            # Try JSON serialization to catch any remaining issues
            try:
                json.dumps(obj)
            except TypeError:
                # If not serializable, convert to string as last resort
                logger.warning(
                    "Non-serializable object in output: %s (%s)",
                    type(obj).__name__,
                    obj,
                )
                return str(obj)
            else:
                return obj

        step_run.output = make_json_safe(merged_output)
        step_run.error = step_error
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
                    },
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

        # Evaluate output-stage assertions against the envelope outputs.
        # This includes generating success messages for passed assertions.
        output_payload = _extract_output_payload_from_envelope(output_envelope)
        if output_payload:
            assertion_findings = _evaluate_output_stage_assertions(
                step_run=step_run,
                output_payload=output_payload,
            )
            if assertion_findings:
                try:
                    ValidationFinding.objects.bulk_create(
                        assertion_findings,
                        batch_size=500,
                    )
                    logger.info(
                        "Created %d assertion findings for step_run %s",
                        len(assertion_findings),
                        step_run.id,
                    )
                except Exception:
                    logger.exception(
                        "Failed to persist assertion findings for step_run %s",
                        step_run.id,
                    )

        # Make outputs available to downstream steps under a namespaced key
        # on the run summary. We use step_run.id as the namespace to avoid
        # collisions. Downstream resolvers can read from
        # run.summary["steps"][<step_run_id>]["signals"].
        if run.summary is None:
            run.summary = {}
        summary_steps = run.summary.get("steps", {})
        summary_steps[str(step_run.id)] = {
            "signals": step_output.get("signals", {}),
        }
        run.summary["steps"] = summary_steps
        run.save(update_fields=["summary"])

        # Only proceed with remaining steps if the current step passed
        remaining_steps = run.workflow.steps.filter(
            order__gt=step_run.step_order,
        ).exists()
        if remaining_steps and step_status == StepStatus.PASSED:
            from validibot.core.tasks import enqueue_validation_run

            # user_id can be NULL if the run was created via API without user context.
            # Pass 0 to signal "no user" - execute() handles this gracefully.
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
            status_mapping = {
                ValidationStatus.SUCCESS: ValidationRunStatus.SUCCEEDED,
                ValidationStatus.FAILED_VALIDATION: ValidationRunStatus.FAILED,
                ValidationStatus.FAILED_RUNTIME: ValidationRunStatus.FAILED,
                ValidationStatus.CANCELLED: ValidationRunStatus.CANCELED,
            }
            error_category_mapping = {
                ValidationStatus.SUCCESS: "",
                ValidationStatus.FAILED_VALIDATION: (
                    ValidationRunErrorCategory.VALIDATION_FAILED
                ),
                ValidationStatus.FAILED_RUNTIME: (
                    ValidationRunErrorCategory.RUNTIME_ERROR
                ),
                ValidationStatus.CANCELLED: "",
            }

            run.status = status_mapping.get(
                output_envelope.status,
                ValidationRunStatus.FAILED,
            )
            run.error_category = error_category_mapping.get(
                output_envelope.status,
                ValidationRunErrorCategory.RUNTIME_ERROR,
            )
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

        # Update the receipt status from PROCESSING to the final status.
        if callback.callback_id and receipt:
            try:
                cb_status = callback.status
                status_str = (
                    cb_status.value
                    if hasattr(cb_status, "value")
                    else str(cb_status)
                )
                receipt.status = status_str
                receipt.validation_run = run
                receipt.save(update_fields=["status", "validation_run"])
                logger.debug(
                    "Updated callback receipt %s to status %s",
                    callback.callback_id,
                    status_str,
                )
            except Exception:
                # If receipt update fails, log but don't fail the request.
                logger.warning(
                    "Failed to update callback receipt for %s",
                    callback.callback_id,
                    exc_info=True,
                )

        logger.info(
            "Successfully processed callback for run %s",
            callback.run_id,
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
