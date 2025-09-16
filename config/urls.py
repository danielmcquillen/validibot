from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin

# Use if async
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include
from django.urls import path
from django.views import defaults as default_views
from django.views.generic import TemplateView
from drf_spectacular.views import SpectacularAPIView
from drf_spectacular.views import SpectacularSwaggerView
from rest_framework.authtoken.views import obtain_auth_token

# from django_github_app.views import AsyncWebhookView

urlpatterns = [
    # Marketing and misc pages...
    path("", include("roscoe.marketing.urls", namespace="marketing")),
    path(
        "about/",
        TemplateView.as_view(template_name="pages/about.html"),
        name="about",
    ),
    # Admin URLs...
    path(settings.ADMIN_URL, admin.site.urls),
    # App URLs...
    path("app/dashboard/", include("roscoe.dashboard.urls", namespace="dashboard")),
    path("app/users/", include("roscoe.users.urls", namespace="users")),
    path("accounts/", include("allauth.urls")),
    path("app/projects/", include("roscoe.projects.urls", namespace="projects")),
    path("app/workflows/", include("roscoe.workflows.urls", namespace="workflows")),
    path("app/tracking/", include("roscoe.tracking.urls", namespace="tracking")),
    path(
        "app/validations/", include("roscoe.validations.urls", namespace="validations")
    ),
    *static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT),
]

if getattr(settings, "GITHUB_APP_ENABLED", False):
    from django_github_app.views import AsyncWebhookView

    urlpatterns.append(path("gh/", AsyncWebhookView.as_view(), name="github-webhook"))

# API URLS
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
