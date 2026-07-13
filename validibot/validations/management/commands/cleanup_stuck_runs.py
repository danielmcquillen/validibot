"""
Management command to mark stuck validation runs as failed.

Validation runs can become "stuck" in RUNNING status if a validator container
crashes without sending a callback, or if the callback fails to reach the
worker service. This watchdog command finds runs that have been in RUNNING
status longer than a threshold and handles them.

For GCP deployments, the command first attempts **reconciliation**: it queries
the Cloud Run Jobs API to determine whether the job actually completed. If the
job succeeded but the callback was lost, it recovers the run by constructing a
synthetic callback and processing it through the normal callback pipeline. This
preserves validation results that would otherwise be lost.

If reconciliation is not possible (non-GCP deployment or API errors), or the
provider is still running after the configured outer runtime deadline, the
command marks the run TIMED_OUT and requests provider cancellation.

Usage:
    python manage.py cleanup_stuck_runs
    python manage.py cleanup_stuck_runs --timeout-minutes 60
    python manage.py cleanup_stuck_runs --dry-run

Environment:
    This command should be scheduled to run periodically (e.g., every 10 minutes)
    via Cloud Scheduler on the worker service.

See also:
    - docs/dev_docs/google_cloud/scheduled-jobs.md for scheduling setup
    - validibot/validations/api/callbacks.py for the normal completion path
"""

import logging
import math
from datetime import timedelta
from http import HTTPStatus

from django.conf import settings
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction
from django.utils import timezone

from validibot.validations.constants import StepStatus
from validibot.validations.constants import ValidationRunErrorCategory
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.models import ValidationRun
from validibot.validations.models import ValidationStepRun
from validibot.validations.services.runners.base import ExecutionStatus
from validibot.validations.services.validation_run import cancel_active_execution
from validibot.validations.services.validation_run import fence_active_execution_attempt

logger = logging.getLogger(__name__)

DEFAULT_VALIDATOR_TIMEOUT_SECONDS = 3600

# Cloud Run occasionally reports a terminal failure without diagnostic text.
# Keep the fallback static and bounded so reconciliation remains truthful
# without exposing provider payloads to end users.
PROVIDER_FAILURE_WITHOUT_DETAILS = (
    "Cloud Run reported a failed execution without diagnostic details"
)

# Max IDs to display in output before truncating
MAX_DISPLAY_IDS = 10


def get_default_timeout_minutes() -> int:
    """Return the configured outer validator timeout rounded up to minutes."""
    timeout_seconds = getattr(
        settings,
        "VALIDATOR_TIMEOUT_SECONDS",
        DEFAULT_VALIDATOR_TIMEOUT_SECONDS,
    )
    return max(1, math.ceil(timeout_seconds / 60))


class Command(BaseCommand):
    help = "Mark validation runs stuck in RUNNING status as TIMED_OUT."

    def add_arguments(self, parser):
        parser.add_argument(
            "--timeout-minutes",
            type=int,
            default=None,
            help=(
                "Consider runs stuck after this many minutes. Defaults to the "
                "configured VALIDATOR_TIMEOUT_SECONDS value "
                f"({get_default_timeout_minutes()} minutes)."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report stuck runs without modifying them",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=100,
            help="Maximum number of runs to process per invocation (default: 100)",
        )

    def handle(self, *args, **options):
        timeout_minutes = options["timeout_minutes"]
        if timeout_minutes is None:
            timeout_minutes = get_default_timeout_minutes()
        if timeout_minutes <= 0:
            raise CommandError("--timeout-minutes must be greater than zero")
        dry_run = options["dry_run"]
        batch_size = options["batch_size"]

        timeout = timedelta(minutes=timeout_minutes)
        cutoff = timezone.now() - timeout

        # Find runs that have been RUNNING for too long
        # We check started_at, not created_at, to measure actual run time
        stuck_runs = ValidationRun.objects.filter(
            status=ValidationRunStatus.RUNNING,
            started_at__lt=cutoff,
        ).order_by("started_at")[:batch_size]

        count = stuck_runs.count()

        if count == 0:
            self.stdout.write(
                self.style.SUCCESS(
                    f"No runs stuck longer than {timeout_minutes} minutes."
                )
            )
            return

        reconciled_ids = []
        profile_rejected_ids = []
        timed_out_ids = []
        error_message = (
            f"Run timed out after {timeout_minutes} minutes - "
            "no callback received from validator. This may indicate the validator "
            "crashed or the callback failed to reach the server."
        )

        for run in stuck_runs:
            # Attempt GCP reconciliation first
            result = self._try_reconcile_gcp_run(run, dry_run=dry_run)
            if result == "reconciled":
                reconciled_ids.append(str(run.id))
                continue
            if result == "profile_rejected":
                profile_rejected_ids.append(str(run.id))
                continue
            # Once the configured outer deadline has elapsed, a provider that
            # still reports RUNNING must be fenced and canceled rather than
            # skipped forever. "not_applicable" and API errors also fall
            # through to the same authoritative timeout decision.

            if dry_run:
                minutes_running = (timezone.now() - run.started_at).total_seconds() / 60
                self.stdout.write(
                    f"  [TIMEOUT] {run.id}: running for {minutes_running:.1f} "
                    f"minutes (workflow={run.workflow_id})"
                )
                timed_out_ids.append(str(run.id))
                continue

            with transaction.atomic():
                # Re-fetch with lock to avoid race conditions
                locked = ValidationRun.objects.select_for_update().get(pk=run.pk)

                # Double-check status hasn't changed since our query
                if locked.status != ValidationRunStatus.RUNNING:
                    continue

                locked.status = ValidationRunStatus.TIMED_OUT
                locked.error_category = ValidationRunErrorCategory.TIMEOUT
                locked.error = error_message
                locked.ended_at = timezone.now()

                if locked.started_at and locked.ended_at:
                    locked.duration_ms = int(
                        (locked.ended_at - locked.started_at).total_seconds() * 1000
                    )

                locked.save(
                    update_fields=[
                        "status",
                        "error_category",
                        "error",
                        "ended_at",
                        "duration_ms",
                    ]
                )
                from validibot.validations.constants import ExecutionAttemptState

                fence_active_execution_attempt(
                    locked,
                    target=ExecutionAttemptState.TIMED_OUT,
                    error_code="run_timed_out",
                    error_message=error_message,
                )
                timed_out_ids.append(str(locked.id))

                workflow_id = locked.workflow_id
                logger.warning(
                    "Marked stuck run as TIMED_OUT",
                    extra={
                        "run_id": str(locked.id),
                        "workflow_id": str(workflow_id) if workflow_id else None,
                        "timeout_minutes": timeout_minutes,
                    },
                )

            # The database decision is committed before provider contact. A
            # cancellation failure is logged by the helper and cannot reopen
            # the terminal run; durable retries arrive with the attempt model.
            from validibot.validations.signals import validation_run_finalized

            validation_run_finalized.send_robust(
                sender=self.__class__,
                validation_run=locked,
            )
            cancel_active_execution(locked)

        # Report results
        if dry_run:
            total = len(reconciled_ids) + len(profile_rejected_ids) + len(timed_out_ids)
            self.stdout.write(
                self.style.WARNING(
                    f"[DRY RUN] {total} stuck run(s): "
                    f"{len(reconciled_ids)} reconcilable, "
                    f"{len(profile_rejected_ids)} profile-rejected, "
                    f"{len(timed_out_ids)} would time out"
                )
            )
        else:
            if reconciled_ids:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Reconciled {len(reconciled_ids)} run(s) from GCP."
                    )
                )
                self._display_ids(reconciled_ids)

            if timed_out_ids:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Marked {len(timed_out_ids)} stuck run(s) as TIMED_OUT "
                        f"(timeout: {timeout_minutes} minutes)."
                    )
                )
                self._display_ids(timed_out_ids)

            if profile_rejected_ids:
                self.stdout.write(
                    self.style.WARNING(
                        "Failed "
                        f"{len(profile_rejected_ids)} run(s) whose runtime "
                        "profile this watchdog cannot process safely."
                    )
                )
                self._display_ids(profile_rejected_ids)

            if not reconciled_ids and not profile_rejected_ids and not timed_out_ids:
                self.stdout.write(self.style.SUCCESS("No runs needed cleanup."))

    def _display_ids(self, ids: list[str]) -> None:
        """Display a truncated list of run IDs."""
        if ids:
            self.stdout.write(f"  IDs: {', '.join(ids[:MAX_DISPLAY_IDS])}")
            if len(ids) > MAX_DISPLAY_IDS:
                extra = len(ids) - MAX_DISPLAY_IDS
                self.stdout.write(f"  ... and {extra} more")

    def _try_reconcile_gcp_run(
        self,
        run: ValidationRun,
        *,
        dry_run: bool = False,
    ) -> str:
        """
        Attempt to reconcile a stuck run via GCP Cloud Run Job status.

        Checks if the run was executed on GCP Cloud Run, queries the job status,
        and either recovers a lost callback or marks the run as failed based on
        the actual job outcome.

        Args:
            run: The stuck ValidationRun.
            dry_run: If True, report what would happen without modifying.

        Returns:
            One of:
            - "reconciled": Run was recovered or marked failed based on GCP status
            - "still_running": Job is still executing on GCP; the caller
              applies the configured timeout fence and requests cancellation
            - "not_applicable": Not a GCP run or missing metadata
            - "error": GCP API call failed (fall through to timeout)
        """
        # 1. Check if deployment is GCP
        if not self._is_gcp_deployment():
            return "not_applicable"

        # 2. Find the active step run with execution metadata
        step_run = self._get_active_step_run(run)
        if not step_run:
            return "not_applicable"

        from validibot.validations.services.execution_attempts import (
            resolve_provider_execution_identity,
        )

        identity = resolve_provider_execution_identity(step_run)
        if identity is None:
            return "not_applicable"
        execution_name = identity.execution_id
        # 3. Query Cloud Run Job status via backend
        try:
            from validibot.validations.services.execution.gcp import GCPExecutionBackend

            backend = GCPExecutionBackend()
            status_response = backend.check_status(execution_name)
        except Exception:
            logger.warning(
                "Failed to check GCP execution status for run %s",
                run.id,
                exc_info=True,
            )
            return "error"

        if status_response is None:
            return "error"

        # 4. Act based on the explicit provider state. Human-readable messages
        # are diagnostics only and must never determine success versus failure.
        execution_status = status_response.execution_status
        if execution_status == ExecutionStatus.FAILED:
            error_message = (
                status_response.error_message or PROVIDER_FAILURE_WITHOUT_DETAILS
            )
            if dry_run:
                self.stdout.write(
                    f"  [RECONCILE-FAIL] {run.id}: Cloud Run job failed "
                    f"({error_message})"
                )
                return "reconciled"

            self._mark_run_failed_from_gcp(
                run,
                error_message,
                attempt=identity.attempt,
            )
            return "reconciled"

        if not status_response.is_complete:
            logger.info(
                "GCP job still running beyond the watchdog deadline for run %s "
                "(execution=%s)",
                run.id,
                execution_name,
            )
            return "still_running"

        if execution_status != ExecutionStatus.SUCCEEDED:
            logger.warning(
                "Cannot safely reconcile provider state %s for run %s",
                execution_status,
                run.id,
            )
            return "error"

        # Job succeeded — attempt to recover via synthetic callback
        if dry_run:
            self.stdout.write(
                f"  [RECONCILE-OK] {run.id}: Cloud Run job succeeded, "
                f"would recover via callback"
            )
            return "reconciled"

        return self._recover_lost_callback(
            run,
            execution_bundle_uri=identity.execution_bundle_uri,
            attempt=identity.attempt,
        )

    def _is_gcp_deployment(self) -> bool:
        """Check if the current deployment target is GCP."""
        try:
            from validibot.core.constants import DeploymentTarget
            from validibot.core.deployment import get_deployment_target

            return get_deployment_target() == DeploymentTarget.GCP
        except Exception:
            return False

    def _get_active_step_run(self, run: ValidationRun) -> ValidationStepRun | None:
        """Find the active (RUNNING) step run for a validation run."""
        return (
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

    def _mark_run_failed_from_gcp(
        self,
        run: ValidationRun,
        error_message: str,
        *,
        attempt,
    ) -> None:
        """Mark a run as failed based on GCP Cloud Run Job failure."""
        error_message = error_message or PROVIDER_FAILURE_WITHOUT_DETAILS

        with transaction.atomic():
            locked = ValidationRun.objects.select_for_update().get(pk=run.pk)
            if locked.status != ValidationRunStatus.RUNNING:
                return

            locked.status = ValidationRunStatus.FAILED
            locked.error_category = ValidationRunErrorCategory.RUNTIME_ERROR
            locked.error = (
                f"Cloud Run Job failed: {error_message}. "
                "The callback was not received — this was recovered by "
                "reconciliation."
            )
            locked.ended_at = timezone.now()

            if locked.started_at and locked.ended_at:
                locked.duration_ms = int(
                    (locked.ended_at - locked.started_at).total_seconds() * 1000
                )

            locked.save(
                update_fields=[
                    "status",
                    "error_category",
                    "error",
                    "ended_at",
                    "duration_ms",
                ]
            )

            logger.warning(
                "Reconciled GCP run as FAILED",
                extra={
                    "run_id": str(locked.id),
                    "error_message": error_message,
                },
            )

        from validibot.validations.constants import ExecutionAttemptState
        from validibot.validations.services.execution_attempts import (
            transition_execution_attempt,
        )

        transition_execution_attempt(
            attempt.pk,
            ExecutionAttemptState.FAILED,
            provider_finished_at=timezone.now(),
            last_error_code="provider_failed",
            last_error=error_message,
        )

        from validibot.validations.signals import validation_run_finalized

        validation_run_finalized.send_robust(
            sender=self.__class__,
            validation_run=locked,
        )

    def _recover_lost_callback(
        self,
        run: ValidationRun,
        *,
        execution_bundle_uri: str,
        attempt,
    ) -> str:
        """
        Recover a completed GCP run by processing a synthetic callback.

        The Cloud Run Job succeeded but the callback was lost. We construct
        a synthetic callback payload and process it through the normal
        ValidationCallbackService pipeline, which handles:
        - Idempotency (via CallbackReceipt)
        - Envelope download
        - Finding persistence and assertion evaluation
        - Run finalization

        Args:
            run: The stuck ValidationRun.
            execution_bundle_uri: Durable bundle location recorded on the attempt.
            attempt: The concrete provider execution being reconciled.

        Returns:
            "reconciled" on success, "error" on failure.
        """
        if not execution_bundle_uri:
            logger.warning(
                "Cannot recover run %s: its attempt has no execution bundle URI",
                run.id,
            )
            return "error"

        # Derive the output envelope URI from the bundle URI
        result_uri = f"{execution_bundle_uri.rstrip('/')}/output.json"

        # Build a synthetic callback payload
        from validibot_shared.validations.envelopes import ValidationStatus

        from validibot.validations.services.execution_attempts import (
            build_attempt_callback_id,
        )

        callback_id = build_attempt_callback_id(attempt)

        callback_payload = {
            "run_id": str(run.id),
            "callback_id": callback_id,
            "status": ValidationStatus.SUCCESS,
            "result_uri": result_uri,
        }

        try:
            from validibot.validations.services.validation_callback import (
                ValidationCallbackService,
            )

            service = ValidationCallbackService()
            response = service.process(payload=callback_payload)

            if response.status_code == HTTPStatus.OK:
                logger.info(
                    "Successfully reconciled run %s via synthetic callback",
                    run.id,
                )
                return "reconciled"

            logger.warning(
                "Synthetic callback returned status %d for run %s: %s",
                response.status_code,
                run.id,
                response.data,
            )
            return "error"  # noqa: TRY300

        except Exception:
            logger.exception(
                "Failed to recover run %s via synthetic callback",
                run.id,
            )
            return "error"
