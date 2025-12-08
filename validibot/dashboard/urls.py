from django.urls import path

from validibot.dashboard import views

app_name = "dashboard"
urlpatterns = [
    path(
        "",
        views.MyDashboardView.as_view(),
        name="my_dashboard",
    ),
    path(
        "widgets/<slug:slug>/",
        views.WidgetDetailView.as_view(),
        name="widget-detail",
    ),
]
