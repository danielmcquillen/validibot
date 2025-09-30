from django.urls import path

from simplevalidations.dashboard import views

app_name = "dashboard"
urlpatterns = [
    path(
        "",
        views.MyDashboardView.as_view(),
        name="my_dashboard",
    ),
]
