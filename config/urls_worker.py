"""
Internal URLConf for APP_ROLE=worker instances.

The worker service is private (IAM-gated) and exposes only API routes, including
the validator callback endpoint. Marketing/UI routes are intentionally omitted.
"""

from django.urls import include
from django.urls import path

from validibot.core.health import health_check

# Internal-only API surface
urlpatterns = [
    # Health check endpoint for container orchestration (Docker, Kubernetes)
    path("health/", health_check, name="health-check"),
    path("api/v1/", include("config.api_internal_router")),
    # auth-token endpoint disabled - users should create API keys via web UI
    # from rest_framework.authtoken.views import obtain_auth_token
    # path("api/v1/auth-token/", obtain_auth_token, name="auth-token"),
    #
    # API docs (schema / Swagger / ReDoc) are intentionally NOT exposed on the
    # worker role. The worker is private and IAM-gated; interactive docs would
    # leak the internal API surface to anyone who reaches the worker origin.
    # The web role serves docs instead (see config/urls_web.py).
    # (ADR 04-23 §hyg.worker_docs_exposed)
]
