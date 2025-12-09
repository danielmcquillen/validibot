"""
API endpoints for scheduled tasks triggered by Cloud Scheduler.

These endpoints wrap Django management commands and are designed to be called
by Cloud Scheduler jobs. Authentication is handled at the Cloud Run IAM level
using OIDC tokens.

Usage:
    Cloud Scheduler calls these endpoints with an OIDC token that Cloud Run
    verifies automatically. No application-level authentication is required.

Example Cloud Scheduler setup:
    gcloud scheduler jobs create http cleanup-idempotency-keys \
      --schedule "0 3 * * *" \
      --time-zone "Australia/Sydney" \
      --uri "https://validibot-worker.run.app/api/v1/scheduled/cleanup-idempotency-keys/" \
      --http-method POST \
      --oidc-service-account-email cloud-scheduler@PROJECT.iam.gserviceaccount.com \
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
