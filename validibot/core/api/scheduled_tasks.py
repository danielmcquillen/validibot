"""
API endpoints for scheduled tasks triggered by Cloud Scheduler.

These endpoints wrap Django management commands and are designed to be called
by Cloud Scheduler jobs. Authentication is handled at the Cloud Run IAM level
using OIDC tokens.

Usage:
    Cloud Scheduler calls these endpoints with an OIDC token that Cloud Run
    verifies automatically. No application-level authentication is required.

Example Cloud Scheduler setup:
    gcloud scheduler jobs create http cleanup-idempotency-keys \\
      --schedule "0 3 * * *" \\
      --time-zone "Australia/Sydney" \\
      --uri "https://worker.run.app/api/v1/scheduled/cleanup-idempotency-keys/" \\
      --http-method POST \\
      --oidc-service-account-email scheduler@PROJECT.iam.gserviceaccount.com \\
      --location australia-southeast1
"""

import logging
from io import StringIO

from django.conf import settings
from django.core.management import call_command
from django.http import Http404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


class ScheduledTaskBaseView(APIView):
    """
    Base class for scheduled task endpoints.

    Authentication is handled by Cloud Run IAM (OIDC tokens from Cloud Scheduler).
    These endpoints are only available on worker instances.
    """

    # Cloud Run IAM performs authentication; DRF auth is disabled here.
    authentication_classes = []
    permission_classes = []

    def check_worker_mode(self):
        """Ensure we're running on a worker instance."""
        if not getattr(settings, "APP_IS_WORKER", False):
            raise Http404


class CleanupIdempotencyKeysView(ScheduledTaskBaseView):
    """
    Clean up expired idempotency keys.

    Idempotency keys expire after 24 hours (configurable via IDEMPOTENCY_KEY_TTL_HOURS).
    This endpoint should be scheduled to run daily.

    URL: POST /api/v1/scheduled/cleanup-idempotency-keys/
    Recommended schedule: Daily at 3 AM
    """

    def post(self, request):
        self.check_worker_mode()

        logger.info("Starting scheduled cleanup of idempotency keys")

        try:
            # Capture command output
            out = StringIO()
            call_command("cleanup_idempotency_keys", stdout=out)
            output = out.getvalue()

            logger.info("Idempotency key cleanup completed: %s", output.strip())

            return Response(
                {
                    "task": "cleanup_idempotency_keys",
                    "status": "completed",
                    "output": output.strip(),
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.exception("Failed to cleanup idempotency keys")
            return Response(
                {
                    "task": "cleanup_idempotency_keys",
                    "status": "failed",
                    "error": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class CleanupCallbackReceiptsView(ScheduledTaskBaseView):
    """
    Clean up old callback receipts.

    Callback receipts are used for idempotency when processing validator callbacks.
    Old receipts (default: 30 days) can be safely deleted.

    URL: POST /api/v1/scheduled/cleanup-callback-receipts/
    Recommended schedule: Weekly on Sunday at 4 AM
    """

    def post(self, request):
        self.check_worker_mode()

        # Allow overriding retention days via request body
        days = request.data.get("days", 30)

        logger.info("Starting scheduled cleanup of callback receipts (days=%d)", days)

        try:
            out = StringIO()
            call_command("cleanup_callback_receipts", f"--days={days}", stdout=out)
            output = out.getvalue()

            logger.info("Callback receipt cleanup completed: %s", output.strip())

            return Response(
                {
                    "task": "cleanup_callback_receipts",
                    "status": "completed",
                    "retention_days": days,
                    "output": output.strip(),
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.exception("Failed to cleanup callback receipts")
            return Response(
                {
                    "task": "cleanup_callback_receipts",
                    "status": "failed",
                    "error": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class ClearSessionsView(ScheduledTaskBaseView):
    """
    Clear expired Django sessions.

    Django's clearsessions command removes expired sessions from the database.
    This is a built-in Django command that should run periodically.

    URL: POST /api/v1/scheduled/clear-sessions/
    Recommended schedule: Daily at 2 AM
    """

    def post(self, request):
        self.check_worker_mode()

        logger.info("Starting scheduled session cleanup")

        try:
            out = StringIO()
            call_command("clearsessions", stdout=out)
            output = out.getvalue() or "Sessions cleared successfully"

            logger.info("Session cleanup completed: %s", output.strip())

            return Response(
                {
                    "task": "clearsessions",
                    "status": "completed",
                    "output": output.strip(),
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.exception("Failed to clear sessions")
            return Response(
                {
                    "task": "clearsessions",
                    "status": "failed",
                    "error": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class PurgeExpiredSubmissionsView(ScheduledTaskBaseView):
    """
    Purge submission content that has passed its retention period.

    This endpoint triggers the purge_expired_submissions management command,
    which removes content from submissions where expires_at < now while
    preserving the submission record for audit purposes.

    URL: POST /api/v1/scheduled/purge-expired-submissions/
    Recommended schedule: Hourly at :00
    """

    def post(self, request):
        self.check_worker_mode()

        # Allow overriding batch parameters via request body
        batch_size = request.data.get("batch_size", 100)
        max_batches = request.data.get("max_batches", 10)

        logger.info(
            "Starting scheduled purge of expired submissions "
            "(batch_size=%d, max_batches=%d)",
            batch_size,
            max_batches,
        )

        try:
            out = StringIO()
            err = StringIO()
            call_command(
                "purge_expired_submissions",
                f"--batch-size={batch_size}",
                f"--max-batches={max_batches}",
                stdout=out,
                stderr=err,
            )
            output = out.getvalue()
            errors = err.getvalue()

            logger.info("Expired submission purge completed: %s", output.strip())

            response_data = {
                "task": "purge_expired_submissions",
                "status": "completed",
                "output": output.strip(),
            }
            if errors:
                response_data["errors"] = errors.strip()

            return Response(response_data, status=status.HTTP_200_OK)
        except Exception as e:
            logger.exception("Failed to purge expired submissions")
            return Response(
                {
                    "task": "purge_expired_submissions",
                    "status": "failed",
                    "error": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class ProcessPurgeRetriesView(ScheduledTaskBaseView):
    """
    Process failed submission purge retries.

    This endpoint triggers the process_purge_retries management command,
    which retries purging submissions that failed on previous attempts
    (e.g., due to GCS unavailability).

    URL: POST /api/v1/scheduled/process-purge-retries/
    Recommended schedule: Every 5 minutes
    """

    def post(self, request):
        self.check_worker_mode()

        # Allow overriding batch size via request body
        batch_size = request.data.get("batch_size", 50)

        logger.info(
            "Starting scheduled processing of purge retries (batch_size=%d)",
            batch_size,
        )

        try:
            out = StringIO()
            err = StringIO()
            call_command(
                "process_purge_retries",
                f"--batch-size={batch_size}",
                stdout=out,
                stderr=err,
            )
            output = out.getvalue()
            errors = err.getvalue()

            logger.info("Purge retry processing completed: %s", output.strip())

            response_data = {
                "task": "process_purge_retries",
                "status": "completed",
                "output": output.strip(),
            }
            if errors:
                response_data["errors"] = errors.strip()

            return Response(response_data, status=status.HTTP_200_OK)
        except Exception as e:
            logger.exception("Failed to process purge retries")
            return Response(
                {
                    "task": "process_purge_retries",
                    "status": "failed",
                    "error": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


class CleanupStuckRunsView(ScheduledTaskBaseView):
    """
    Mark stuck validation runs as FAILED.

    Validation runs can become "stuck" in RUNNING status if a validator
    container crashes without sending a callback, or if the callback fails.
    This watchdog finds runs that have been RUNNING longer than a threshold
    and marks them as FAILED.

    URL: POST /api/v1/scheduled/cleanup-stuck-runs/
    Recommended schedule: Every 10 minutes

    Request body (optional):
        timeout_minutes: int - Consider runs stuck after this many minutes (default: 30)
        batch_size: int - Max runs to process (default: 100)
    """

    def post(self, request):
        self.check_worker_mode()

        # Allow overriding parameters via request body
        timeout_minutes = request.data.get("timeout_minutes", 30)
        batch_size = request.data.get("batch_size", 100)

        logger.info(
            "Starting scheduled cleanup of stuck runs "
            "(timeout_minutes=%d, batch_size=%d)",
            timeout_minutes,
            batch_size,
        )

        try:
            out = StringIO()
            call_command(
                "cleanup_stuck_runs",
                f"--timeout-minutes={timeout_minutes}",
                f"--batch-size={batch_size}",
                stdout=out,
            )
            output = out.getvalue()

            logger.info("Stuck run cleanup completed: %s", output.strip())

            return Response(
                {
                    "task": "cleanup_stuck_runs",
                    "status": "completed",
                    "timeout_minutes": timeout_minutes,
                    "output": output.strip(),
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.exception("Failed to cleanup stuck runs")
            return Response(
                {
                    "task": "cleanup_stuck_runs",
                    "status": "failed",
                    "error": str(e),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
