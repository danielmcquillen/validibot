"""URL patterns for the advanced analytics dashboard.

Mounted at ``/app/analytics/`` by ``config/urls_web.py`` under the
``analytics`` namespace. The dashboard is the only URL today; future
reports can be added as additional patterns without a new app.
"""

from django.urls import path

from validibot.analytics import views

app_name = "analytics"

urlpatterns = [
    path(
        "",
        views.AdvancedAnalyticsDashboardView.as_view(),
        name="dashboard",
    ),
]
