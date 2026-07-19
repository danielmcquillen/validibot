"""Worker-only renewal endpoint for attempt-scoped GCS credentials.

Cloud Run validators receive a short-lived downscoped token at dispatch. A
long-running attempt can renew that token only by presenting its callback nonce
while the durable attempt remains active. Terminal attempts are fenced before
any new storage authority is issued.
"""

from __future__ import annotations

import logging

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response

from validibot.core.api.worker import WorkerOnlyAPIView
from validibot.validations.constants import EXECUTION_ATTEMPT_ACTIVE_STATES
from validibot.validations.services.cloud_run.gcs_runtime_capabilities import (
    issue_attempt_gcs_runtime_capability,
)
from validibot.validations.services.execution_attempts import resolve_callback_attempt
from validibot.validations.services.execution_attempts import (
    verify_attempt_callback_nonce,
)

logger = logging.getLogger(__name__)


class ValidationStorageCapabilityRefreshView(WorkerOnlyAPIView):
    """Renew one prefix-identical GCS token for an active execution attempt."""

    def post(self, request):
        """Authenticate attempt proof, enforce lifecycle fencing, and renew."""
        run_id = str(request.data.get("run_id", ""))
        callback_id = str(request.data.get("callback_id", ""))
        callback_nonce = str(request.data.get("callback_nonce", ""))
        if not run_id or not callback_id or not callback_nonce:
            return Response(
                {"detail": "Attempt capability proof is incomplete."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            attempt = resolve_callback_attempt(callback_id, run_id=run_id)
        except (TypeError, ValueError):
            attempt = None
        if attempt is None or not verify_attempt_callback_nonce(
            attempt,
            callback_nonce,
        ):
            return Response(
                {"detail": "Attempt capability proof was rejected."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if attempt.state not in EXECUTION_ATTEMPT_ACTIVE_STATES:
            return Response(
                {"detail": "Execution attempt is terminal."},
                status=status.HTTP_409_CONFLICT,
            )
        if not getattr(settings, "GCS_VALIDATOR_ATTEMPT_CAPABILITIES_ENABLED", False):
            return Response(
                {"detail": "Attempt-scoped GCS capabilities are disabled."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            refresh_url = (
                f"{settings.WORKER_URL.rstrip('/')}"
                "/api/v1/validation-storage-capabilities/refresh/"
            )
            capability = issue_attempt_gcs_runtime_capability(
                execution_bundle_uri=attempt.execution_bundle_uri,
                project_id=settings.GCP_PROJECT_ID,
                refresh_url=refresh_url,
            )
        except Exception:
            logger.exception(
                "Could not renew GCS capability for execution attempt %s",
                attempt.pk,
            )
            return Response(
                {"detail": "Attempt capability could not be issued."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # Issuance makes an external STS call and intentionally happens without
        # holding a database lock. Re-read durable state before returning the
        # bearer token so a concurrent completion/cancellation during that call
        # discards the token instead of delivering fresh post-terminal authority.
        attempt.refresh_from_db(fields=["state"])
        if attempt.state not in EXECUTION_ATTEMPT_ACTIVE_STATES:
            return Response(
                {"detail": "Execution attempt is terminal."},
                status=status.HTTP_409_CONFLICT,
            )

        response = Response(
            {
                "access_token": capability.access_token,
                "expires_at": capability.expires_at.isoformat().replace("+00:00", "Z"),
                "allowed_prefix": capability.allowed_prefix,
            }
        )
        response["Cache-Control"] = "no-store"
        response["Pragma"] = "no-cache"
        return response
