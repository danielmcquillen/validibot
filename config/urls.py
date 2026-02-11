from django.conf import settings
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.urls import include
from django.urls import path
from django.views import defaults as default_views

# Branch URLConf based on APP_ROLE. Web instances serve UI/marketing only;
# worker instances expose only the API surface (IAM-gated).
_is_worker = bool(getattr(settings, "APP_IS_WORKER", False)) or (
    getattr(settings, "APP_ROLE", "web").lower() == "worker"
)
if _is_worker:
    from config.urls_worker import urlpatterns
else:
    from config.urls_web import urlpatterns

# Debug helpers are appended for local development when DEBUG is on.
if settings.DEBUG:
    urlpatterns += staticfiles_urlpatterns()
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
