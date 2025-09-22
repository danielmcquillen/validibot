from django.urls import path

from roscoe.validations import views

app_name = "validations"

urlpatterns = [
    path(
        "",
        views.ValidationRunListView.as_view(),
        name="validation_list",
    ),
    path(
        "<uuid:pk>/",
        views.ValidationRunDetailView.as_view(),
        name="validation_detail",
    ),
    path(
        "<uuid:pk>/delete/",
        views.ValidationRunDeleteView.as_view(),
        name="validation_delete",
    ),
]
