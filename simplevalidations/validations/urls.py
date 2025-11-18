from django.urls import path

from simplevalidations.validations import views

app_name = "validations"

urlpatterns = [
    path(
        "library/",
        views.ValidationLibraryView.as_view(),
        name="validation_library",
    ),
    path(
        "library/custom/new/",
        views.CustomValidatorCreateView.as_view(),
        name="custom_validator_create",
    ),
    path(
        "library/custom/<slug:slug>/edit/",
        views.CustomValidatorUpdateView.as_view(),
        name="custom_validator_update",
    ),
    path(
        "library/custom/<slug:slug>/delete/",
        views.CustomValidatorDeleteView.as_view(),
        name="custom_validator_delete",
    ),
    path(
        "library/custom/<int:pk>/",
        views.ValidatorDetailView.as_view(),
        name="validator_detail",
    ),
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
