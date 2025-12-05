"""
Public-facing URLConf for APP_ROLE=web instances.

These routes serve marketing pages and the application UI only. API routes are
omitted here to keep the web service surface area small; APIs live on the worker
service (APP_ROLE=worker) behind IAM.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.sitemaps.views import sitemap
from django.urls import include
from django.urls import path
from django.views.decorators.cache import cache_page
from django.views.generic import TemplateView

from simplevalidations.blog.sitemaps import BlogPostSitemap
from simplevalidations.core import views as core_views
from simplevalidations.core.views import jwks_view
from simplevalidations.marketing import views as marketing_views
from simplevalidations.marketing.sitemaps import MarketingStaticViewSitemap
from simplevalidations.workflows import views as workflow_views

sitemaps = {
    "marketing": MarketingStaticViewSitemap(),
    "blog": BlogPostSitemap(),
}

urlpatterns = [
    path(".well-known/jwks.json", jwks_view, name="jwks"),
    path("", include("simplevalidations.marketing.urls", namespace="marketing")),
    path("robots.txt", marketing_views.robots_txt, name="robots"),
    path(
        "sitemap.xml",
        cache_page(60 * 60)(sitemap),
        {"sitemaps": sitemaps},
        name="sitemap",
    ),
    path(
        "about/",
        TemplateView.as_view(template_name="pages/about.html"),
        name="about",
    ),
    path(
        "workflows/",
        workflow_views.PublicWorkflowListView.as_view(),
        name="public_workflow_list",
    ),
    path(
        "workflows/<uuid:workflow_uuid>/info/",
        workflow_views.PublicWorkflowInfoView.as_view(),
        name="workflow_public_info",
    ),
    path(settings.ADMIN_URL, admin.site.urls),
    path("app/core/", include("simplevalidations.core.urls", namespace="core")),
    *static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT),
]

if settings.ENABLE_APP:
    urlpatterns += [
        path("app/", core_views.app_home_redirect, name="app-home"),
        path(
            "app/dashboard/",
            include("simplevalidations.dashboard.urls", namespace="dashboard"),
        ),
        path(
            "app/users/",
            include("simplevalidations.users.urls", namespace="users"),
        ),
        path(
            "app/projects/",
            include("simplevalidations.projects.urls", namespace="projects"),
        ),
        path(
            "app/members/",
            include("simplevalidations.members.urls", namespace="members"),
        ),
        path(
            "app/workflows/",
            include("simplevalidations.workflows.urls", namespace="workflows"),
        ),
        path(
            "app/tracking/",
            include("simplevalidations.tracking.urls", namespace="tracking"),
        ),
        path(
            "app/validations/",
            include("simplevalidations.validations.urls", namespace="validations"),
        ),
        path(
            "app/help/",
            include("simplevalidations.help.urls", namespace="help"),
        ),
        path(
            "app/notifications/",
            include("simplevalidations.notifications.urls", namespace="notifications"),
        ),
    ]

if settings.ACCOUNT_ALLOW_LOGIN:
    urlpatterns.append(path("accounts/", include("allauth.urls")))

if getattr(settings, "GITHUB_APP_ENABLED", False):
    from django_github_app.views import AsyncWebhookView

    urlpatterns.append(path("gh/", AsyncWebhookView.as_view(), name="github-webhook"))

# Public API surface (available on web). Internal-only endpoints are on worker.
if settings.ENABLE_API:
    from drf_spectacular.views import SpectacularAPIView
    from drf_spectacular.views import SpectacularSwaggerView
    from rest_framework.authtoken.views import obtain_auth_token

    urlpatterns += [
        path("api/v1/", include("config.api_router")),
        path("api/v1/auth-token/", obtain_auth_token, name="auth-token"),
        path("api/v1/schema/", SpectacularAPIView.as_view(), name="api-schema"),
        path(
            "api/v1/docs/",
            SpectacularSwaggerView.as_view(url_name="api-schema"),
            name="api-docs",
        ),
    ]
