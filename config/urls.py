from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.sitemaps.views import sitemap

# Use if async
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include, path
from django.views import defaults as default_views
from django.views.decorators.cache import cache_page
from django.views.generic import TemplateView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView
from rest_framework.authtoken.views import obtain_auth_token

from simplevalidations.blog.sitemaps import BlogPostSitemap
from simplevalidations.core import views as core_views
from simplevalidations.core.views import jwks_view
from simplevalidations.marketing import views as marketing_views
from simplevalidations.marketing.sitemaps import MarketingStaticViewSitemap
from simplevalidations.workflows import views as workflow_views

# from django_github_app.views import AsyncWebhookView

sitemaps = {
    "marketing": MarketingStaticViewSitemap(),
    "blog": BlogPostSitemap(),
}

urlpatterns = [
    # Public key
    path(".well-known/jwks.json", jwks_view, name="jwks"),
    # Marketing and misc pages...
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
    # Admin URLs...
    path(settings.ADMIN_URL, admin.site.urls),
    # App URLs...
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

# API URLS
if settings.ENABLE_API:
    urlpatterns += [
        # API base url
        path("api/v1/", include("config.api_router")),
        # DRF auth token
        path("api/v1/auth-token/", obtain_auth_token, name="obtain_auth_token"),
        path("api/v1/schema/", SpectacularAPIView.as_view(), name="api-schema"),
        path(
            "api/v1/docs/",
            SpectacularSwaggerView.as_view(url_name="api-schema"),
            name="api-docs",
        ),
    ]

if settings.DEBUG:
    # Static file serving when using Gunicorn + Uvicorn for local web socket development
    urlpatterns += staticfiles_urlpatterns()

    # This allows the error pages to be debugged during development, just visit
    # these url in browser to see how these error pages look like.
    urlpatterns += [
        path(
            "400/",
            default_views.bad_request,
            kwargs={"exception": Exception("Bad Request!")},
        ),
        path(
            "403/",
            default_views.permission_denied,
            kwargs={"exception": Exception("Permission Denied")},
        ),
        path(
            "404/",
            default_views.page_not_found,
            kwargs={"exception": Exception("Page not Found")},
        ),
        path("500/", default_views.server_error),
    ]
    if "debug_toolbar" in settings.INSTALLED_APPS:
        import debug_toolbar

        urlpatterns = [
            path("__debug__/", include(debug_toolbar.urls)),
            *urlpatterns,
        ]
