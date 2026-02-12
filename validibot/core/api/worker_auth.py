"""
Shared-secret authentication for worker-only API endpoints.

On GCP, Cloud Run IAM handles authentication at the infrastructure level.
On Docker Compose, there is no equivalent infrastructure-level auth, so
worker endpoints are protected by a shared API key (WORKER_API_KEY).

The key is optional: if WORKER_API_KEY is not set, authentication is skipped
(for GCP and test environments where infrastructure handles auth). When set,
callers must include it in the Authorization header::

    Authorization: Worker-Key <key>

This follows the same pattern as Sentry's SystemToken, using Django's
constant_time_compare() to prevent timing attacks.

See: https://github.com/getsentry/sentry/blob/master/src/sentry/auth/system.py
"""

import logging

from django.conf import settings
from django.utils.crypto import constant_time_compare
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

logger = logging.getLogger(__name__)

WORKER_KEY_HEADER_KEYWORD = "Worker-Key"


class WorkerKeyAuthentication(BaseAuthentication):
    """
    Shared-secret authentication for internal worker endpoints.

    Checks the Authorization header for a Worker-Key token that matches
    the configured WORKER_API_KEY setting. If WORKER_API_KEY is not set,
    authentication is skipped (allowing GCP deployments to rely on
    Cloud Run IAM instead).

    Header format::

        Authorization: Worker-Key <key>
    """

    def authenticate(self, request):
        """
        Validate the worker API key from the Authorization header.

        Returns None if WORKER_API_KEY is not configured (skip auth).
        Returns (None, None) if the key matches (authenticated, no user).
        Raises AuthenticationFailed if the key is wrong or missing.
        """
        configured_key = getattr(settings, "WORKER_API_KEY", "")
        if not configured_key:
            # No key configured - skip authentication (GCP/test path).
            return None

        auth_header = request.META.get("HTTP_AUTHORIZATION", "")
        if not auth_header:
            logger.warning(
                "Worker endpoint called without Authorization header "
                "(WORKER_API_KEY is configured)",
            )
            raise AuthenticationFailed("Worker API key required.")

        # Parse "Worker-Key <key>" format (scheme + token)
        parts = auth_header.split(" ", 1)
        expected_parts = 2
        if len(parts) != expected_parts or parts[0] != WORKER_KEY_HEADER_KEYWORD:
            raise AuthenticationFailed(
                "Invalid authorization header. Expected: "
                "Authorization: Worker-Key <key>",
            )

        provided_key = parts[1]
        if not constant_time_compare(provided_key, configured_key):
            logger.warning("Worker endpoint called with invalid API key")
            raise AuthenticationFailed("Invalid worker API key.")

        # Authenticated as infrastructure caller (no Django user).
        return (None, "worker-key")
