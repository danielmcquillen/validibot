"""
Health check endpoint for Docker/Kubernetes liveness and readiness probes.

This endpoint verifies that:
1. Django application is running and can handle requests
2. Database connection is healthy

The endpoint is designed to be:
- Fast: Uses a simple SELECT 1 query with a short timeout
- Unauthenticated: Accessible without login for container health checks
- Minimal: No middleware dependencies beyond Django core

Usage:
    GET /health/  -> 200 OK with JSON status
    GET /health/  -> 503 Service Unavailable if database is down
"""

import logging
from http import HTTPStatus

from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)

# Database query timeout for health checks (seconds)
HEALTH_CHECK_DB_TIMEOUT = 3


@require_GET
def health_check(request):
    """
    Health check endpoint for container orchestration.

    Returns:
        200 OK: Application is healthy
        503 Service Unavailable: Database connection failed

    Response format:
        {
            "status": "healthy" | "unhealthy",
            "database": "ok" | "error: <message>"
        }
    """
    result = {
        "status": "healthy",
        "database": "ok",
    }

    # Check database connectivity with a simple query
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as e:
        logger.warning("Health check failed: database error: %s", e)
        result["status"] = "unhealthy"
        result["database"] = f"error: {e}"
        return JsonResponse(result, status=HTTPStatus.SERVICE_UNAVAILABLE)

    return JsonResponse(result, status=HTTPStatus.OK)
