"""
HTTP callback client for validator containers.

Provides utilities for POSTing validation completion callbacks back to the
Cloud Run Service (Django).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from sv_shared.validations.envelopes import ValidationCallback
from sv_shared.validations.envelopes import ValidationStatus

logger = logging.getLogger(__name__)


def post_callback(
    callback_url: str,
    callback_token: str,
    run_id: str,
    status: ValidationStatus,
    result_uri: str,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    """
    POST a validation completion callback to the Cloud Run Service.

    Args:
        callback_url: Django callback endpoint URL
        callback_token: JWT token for authentication
        run_id: Validation run ID
        status: Validation status (SUCCESS, FAILED_VALIDATION, etc.)
        result_uri: GCS URI to output.json
        timeout_seconds: HTTP request timeout

    Returns:
        Response JSON from Django

    Raises:
        httpx.HTTPStatusError: If callback request fails
    """
    logger.info("POSTing callback for run_id=%s to %s", run_id, callback_url)

    callback = ValidationCallback(
        callback_token=callback_token,
        run_id=run_id,
        status=status,
        result_uri=result_uri,
    )

    with httpx.Client(timeout=timeout_seconds) as client:
        response = client.post(
            callback_url,
            json=callback.model_dump(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {callback_token}",
            },
        )

        response.raise_for_status()

        logger.info(
            "Callback successful (run_id=%s, status=%d)",
            run_id,
            response.status_code,
        )

        return response.json()
