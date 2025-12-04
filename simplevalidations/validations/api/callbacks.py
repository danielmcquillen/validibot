"""
Callback API endpoint for Cloud Run Job validators.

This module handles ValidationCallback POSTs from validator containers.
It verifies the JWT token, downloads the output envelope from GCS, and
updates the ValidationRun in the database.

Design: Simple APIView with clear error handling. No complex permissions.
"""

import logging
from datetime import UTC
from datetime import datetime

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from sv_shared.energyplus.envelopes import EnergyPlusOutputEnvelope
from sv_shared.validations.envelopes import ValidationCallback
from sv_shared.validations.envelopes import ValidationStatus

from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.services.cloud_run.gcs_client import (
    download_envelope,
)
from simplevalidations.validations.services.cloud_run.token_service import (
    verify_callback_token,
)

logger = logging.getLogger(__name__)


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
                kms_key_name = settings.VALIDATOR_CALLBACK_KMS_KEY
                token_payload = verify_callback_token(
                    callback.callback_token,
                    kms_key_name,
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

            # Download the output envelope from GCS
            # Determine the envelope class based on validator type
            if run.validator.type == "energyplus":
                envelope_class = EnergyPlusOutputEnvelope
            else:
                logger.error("Unsupported validator type: %s", run.validator.type)
                return Response(
                    {"error": f"Unsupported validator type: {run.validator.type}"},
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

            # Map ValidationStatus to ValidationRunStatus
            status_mapping = {
                ValidationStatus.SUCCESS: ValidationRunStatus.SUCCEEDED,
                ValidationStatus.FAILED_VALIDATION: ValidationRunStatus.FAILED,
                ValidationStatus.FAILED_RUNTIME: ValidationRunStatus.FAILED,
                ValidationStatus.CANCELLED: ValidationRunStatus.CANCELED,
            }

            # Update ValidationRun with results
            run.status = status_mapping.get(
                output_envelope.status,
                ValidationRunStatus.FAILED,
            )

            # Set timestamps
            if output_envelope.timing.completed_at:
                run.ended_at = datetime.fromisoformat(
                    output_envelope.timing.completed_at.replace("Z", "+00:00"),
                )
            else:
                run.ended_at = datetime.now(tz=UTC)

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

            run.save()

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
