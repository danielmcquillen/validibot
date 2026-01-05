"""
API endpoint for executing validation runs.

This endpoint is called by Cloud Tasks to process validation runs on the
worker instance. It receives a validation_run_id and optional resume_from_step,
then delegates to ValidationRunService.execute().

Architecture:
    Cloud Tasks -> Worker Instance -> This View -> ValidationRunService.execute()

Authentication is handled by Cloud Run IAM (OIDC tokens from Cloud Tasks).
No application-level authentication is required.

See ADR-001 for detailed architecture documentation.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.http import Http404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from validibot.validations.services.validation_run import ValidationRunService

logger = logging.getLogger(__name__)


class ExecuteValidationRunView(APIView):
    """
    Execute a validation run from a Cloud Tasks delivery.

    This endpoint is the worker-side receiver for validation run execution tasks.
    It's called by Cloud Tasks with a JSON payload containing:
    - validation_run_id: ID of the ValidationRun to execute
    - user_id: ID of the user who initiated the run
    - resume_from_step: (optional) Step order to resume from

    Authentication:
        Cloud Run IAM performs authentication via OIDC token.
        DRF authentication is disabled for this endpoint.

    Error Handling:
        Returns 200 OK for all business logic outcomes (success, failure,
        idempotent skip) so that Cloud Tasks doesn't retry. Returns 500 for
        infrastructure errors (database connection, etc.) which should
        trigger a retry.

    URL: POST /api/v1/execute-validation-run/
    """

    # Cloud Run IAM performs authentication; DRF auth is disabled here.
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        """
        Process a validation run execution task.

        Request body (JSON):
            {
                "validation_run_id": 123,
                "user_id": 456,
                "resume_from_step": null  // or int for resume
            }

        Returns:
            200 OK: Task completed (validation succeeded, failed, or was skipped)
            500 Internal Server Error: Infrastructure error (triggers Cloud Tasks retry)
        """
        # Only available on worker instances
        if not getattr(settings, "APP_IS_WORKER", False):
            raise Http404

        # Parse request data
        validation_run_id = request.data.get("validation_run_id")
        user_id = request.data.get("user_id")
        resume_from_step = request.data.get("resume_from_step")

        if not validation_run_id:
            logger.error("Missing validation_run_id in request")
            return Response(
                {"error": "validation_run_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # user_id is optional - runs can be created without user context (e.g., API)
        # A value of 0 signals "no user" from resume callbacks where run.user_id was NULL
        if user_id == 0:
            user_id = None

        logger.info(
            "Received execute-validation-run task: validation_run_id=%s "
            "user_id=%s resume_from_step=%s",
            validation_run_id,
            user_id,
            resume_from_step,
        )

        try:
            service = ValidationRunService()
            result = service.execute(
                validation_run_id=validation_run_id,
                user_id=user_id,
                metadata=None,
                resume_from_step=resume_from_step,
            )

            logger.info(
                "Validation run %s execution completed: status=%s",
                validation_run_id,
                result.status,
            )

            # Always return 200 for business logic outcomes
            # This tells Cloud Tasks the task was processed successfully
            return Response(
                {
                    "validation_run_id": validation_run_id,
                    "status": result.status,
                    "error": result.error or "",
                },
                status=status.HTTP_200_OK,
            )

        except Exception as exc:
            # Log the exception and return 500 to trigger Cloud Tasks retry
            logger.exception(
                "Failed to execute validation run %s",
                validation_run_id,
            )
            return Response(
                {
                    "validation_run_id": validation_run_id,
                    "error": str(exc),
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
