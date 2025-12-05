"""
Internal URLConf for APP_ROLE=worker instances.

The worker service is private (IAM-gated) and exposes only API routes, including
the validator callback endpoint. Marketing/UI routes are intentionally omitted.
"""

from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token

from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

# Internal-only API surface
urlpatterns = [
    path("api/v1/", include("config.api_internal_router")),
    path("api/v1/auth-token/", obtain_auth_token, name="auth-token"),
    path("api/v1/schema/", SpectacularAPIView.as_view(), name="api-schema"),
    path(
        "api/v1/docs/",
        SpectacularSwaggerView.as_view(url_name="api-schema"),
        name="api-docs",
    ),
]
