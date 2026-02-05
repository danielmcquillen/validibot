"""
Internal URLConf for APP_ROLE=worker instances.

The worker service is private (IAM-gated) and exposes only API routes, including
the validator callback endpoint. Marketing/UI routes are intentionally omitted.
"""

from django.urls import include
from django.urls import path
from drf_spectacular.views import SpectacularAPIView
from drf_spectacular.views import SpectacularRedocView
from drf_spectacular.views import SpectacularSwaggerView

from validibot.core.health import health_check

# Internal-only API surface
urlpatterns = [
    # Health check endpoint for container orchestration (Docker, Kubernetes)
    path("health/", health_check, name="health-check"),
    path("api/v1/", include("config.api_internal_router")),
    # auth-token endpoint disabled - users should create API keys via web UI
    # from rest_framework.authtoken.views import obtain_auth_token
    # path("api/v1/auth-token/", obtain_auth_token, name="auth-token"),
    path("api/v1/schema/", SpectacularAPIView.as_view(), name="api-schema"),
    path(
        "api/v1/docs/",
        SpectacularSwaggerView.as_view(url_name="api-schema"),
        name="api-docs",
    ),
    path(
        "api/v1/redoc/",
        SpectacularRedocView.as_view(url_name="api-schema"),
        name="api-redoc",
    ),
]
