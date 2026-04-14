"""
Public-facing URLConf for APP_ROLE=web instances.

These routes serve the application UI only. Marketing pages are served from
a separate marketing site. API routes are omitted here to keep the web
service surface area small; APIs live on the worker service (APP_ROLE=worker)
behind IAM.
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include
from django.urls import path

from validibot.core import views as core_views
from validibot.core.health import deep_health_check
from validibot.core.health import health_check
from validibot.workflows import views as workflow_views

urlpatterns = [
    # Language switching — must live outside i18n_patterns so it is always reachable
    path("i18n/", include("django.conf.urls.i18n")),
    # Health check endpoint for container orchestration (Docker, Kubernetes)
    path("health/", health_check, name="health-check"),
    path("health/deep/", deep_health_check, name="deep-health-check"),
    path("", include("validibot.home.urls", namespace="home")),
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
    path(
        "workflows/invite/<uuid:token>/",
        workflow_views.WorkflowInviteAcceptView.as_view(),
        name="workflow_invite_accept",
    ),
    path(settings.ADMIN_URL, admin.site.urls),
    path("app/core/", include("validibot.core.urls", namespace="core")),
    *static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT),
]

if settings.ENABLE_APP:
    urlpatterns += [
        path("app/", core_views.app_home_redirect, name="app-home"),
        path(
            "app/dashboard/",
            include("validibot.dashboard.urls", namespace="dashboard"),
        ),
        path(
            "app/users/",
            include("validibot.users.urls", namespace="users"),
        ),
        path(
            "app/projects/",
            include("validibot.projects.urls", namespace="projects"),
        ),
        path(
            "app/members/",
            include("validibot.members.urls", namespace="members"),
        ),
        path(
            "app/workflows/",
            include("validibot.workflows.urls", namespace="workflows"),
        ),
        path(
            "app/tracking/",
            include("validibot.tracking.urls", namespace="tracking"),
        ),
        path(
            "app/validations/",
            include("validibot.validations.urls", namespace="validations"),
        ),
        path(
            "app/help/",
            include("validibot.help.urls", namespace="help"),
        ),
        path(
            "app/notifications/",
            include("validibot.notifications.urls", namespace="notifications"),
        ),
    ]

# ── Validibot Pro routes ──────────────────────────────────────────────
# Pro-owned URLs only exist when the app is activated in INSTALLED_APPS.
# That keeps community-only deployments from advertising routes whose
# templates and views live in the commercial package.
if "validibot_pro" in settings.INSTALLED_APPS:
    from validibot_pro.urls import pro_urlpatterns

    urlpatterns += pro_urlpatterns

if settings.ACCOUNT_ALLOW_LOGIN:
    # ── MFA index redirect ────────────────────────────────────────────
    # Allauth ships an ``/accounts/2fa/`` landing page (URL name
    # ``mfa_index``) that duplicates our own Security settings page at
    # ``/app/users/security/``, which is the canonical entry point and
    # is styled to match the rest of the app. Allauth's MFA flows
    # hard-code ``reverse("mfa_index")`` as their post-action redirect
    # (e.g. after deactivating TOTP), so we preempt the URL name here —
    # Django's resolver picks the first match, and our override is
    # registered before the allauth include below.
    from django.views.generic import RedirectView

    urlpatterns.append(
        path(
            "accounts/2fa/",
            RedirectView.as_view(pattern_name="users:security", permanent=False),
            name="mfa_index",
        ),
    )
    urlpatterns.append(path("accounts/", include("allauth.urls")))

# Public API surface (available on web). Internal-only endpoints are on worker.
if settings.ENABLE_API:
    from drf_spectacular.views import SpectacularAPIView
    from drf_spectacular.views import SpectacularRedocView
    from drf_spectacular.views import SpectacularSwaggerView

    urlpatterns += [
        path("api/v1/", include("config.api_router")),
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
