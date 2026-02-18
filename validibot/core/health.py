"""
Health check endpoints for container orchestration and deployment verification.

Provides two endpoints:

GET /health/
    Lightweight liveness/readiness probe. Unauthenticated, fast (SELECT 1).
    Used by Docker, Kubernetes, and Cloud Run health checks.

GET /health/deep/
    Comprehensive health check for post-deployment verification.
    Requires superuser authentication. Checks database, cache, storage,
    roles, validators, and security settings.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from io import StringIO

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.http import HttpResponseForbidden
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


@login_required
@require_GET
def deep_health_check(request):
    """
    Comprehensive health check for post-deployment verification.

    Requires superuser access. Checks all critical system components
    and returns structured JSON results.

    Returns:
        200 OK: All checks passed
        503 Service Unavailable: One or more checks failed
        403 Forbidden: Non-superuser access
    """
    if not request.user.is_superuser:
        return HttpResponseForbidden("Superuser access required.")

    checks = {}
    has_errors = False

    # Database
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = {"status": "ok"}
    except Exception as e:
        checks["database"] = {"status": "error", "detail": str(e)}
        has_errors = True

    # Migrations
    try:
        out = StringIO()
        call_command("showmigrations", "--plan", stdout=out)
        plan = out.getvalue()
        unapplied = [
            line for line in plan.splitlines() if line.strip().startswith("[ ]")
        ]
        if unapplied:
            checks["migrations"] = {
                "status": "warning",
                "detail": f"{len(unapplied)} unapplied migration(s)",
                "unapplied": unapplied[:5],
            }
        else:
            checks["migrations"] = {"status": "ok"}
    except Exception as e:
        checks["migrations"] = {"status": "error", "detail": str(e)}
        has_errors = True

    # Cache
    try:
        cache.set("_health_check", "ok", timeout=10)
        val = cache.get("_health_check")
        if val == "ok":
            checks["cache"] = {"status": "ok"}
        else:
            checks["cache"] = {"status": "error", "detail": "Cache set/get mismatch"}
            has_errors = True
        cache.delete("_health_check")
    except Exception as e:
        checks["cache"] = {"status": "error", "detail": str(e)}
        has_errors = True

    # Storage
    try:
        from django.core.files.storage import default_storage

        checks["storage"] = {
            "status": "ok",
            "backend": type(default_storage).__name__,
        }
    except Exception as e:
        checks["storage"] = {"status": "error", "detail": str(e)}
        has_errors = True

    # Site configuration
    try:
        from django.contrib.sites.models import Site

        site = Site.objects.get_current()
        checks["site"] = {
            "status": "ok",
            "domain": site.domain,
        }
    except Exception as e:
        checks["site"] = {"status": "error", "detail": str(e)}
        has_errors = True

    # Roles
    try:
        from validibot.users.models import Role

        role_count = Role.objects.count()
        if role_count > 0:
            checks["roles"] = {"status": "ok", "count": role_count}
        else:
            checks["roles"] = {"status": "error", "detail": "No roles found"}
            has_errors = True
    except Exception as e:
        checks["roles"] = {"status": "error", "detail": str(e)}
        has_errors = True

    # Validators
    try:
        from validibot.validators.models import Validator

        validator_count = Validator.objects.filter(is_active=True).count()
        checks["validators"] = {"status": "ok", "active_count": validator_count}
    except Exception as e:
        checks["validators"] = {"status": "error", "detail": str(e)}
        has_errors = True

    # Security basics
    security_warnings = []
    if settings.DEBUG:
        security_warnings.append("DEBUG is True")
    if not getattr(settings, "SECURE_SSL_REDIRECT", False):
        security_warnings.append("SECURE_SSL_REDIRECT is False")
    if security_warnings:
        checks["security"] = {"status": "warning", "warnings": security_warnings}
    else:
        checks["security"] = {"status": "ok"}

    overall_status = "unhealthy" if has_errors else "healthy"
    http_status = HTTPStatus.SERVICE_UNAVAILABLE if has_errors else HTTPStatus.OK

    return JsonResponse(
        {"status": overall_status, "checks": checks},
        status=http_status,
    )
