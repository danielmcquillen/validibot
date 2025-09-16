from django.urls import path

from roscoe.workflows import views

app_name = "workflows"

urlpatterns = [
    path("", views.WorkflowListView.as_view(), name="workflow_list"),
    path("new/", views.WorkflowCreateView.as_view(), name="workflow_create"),
    path("<int:pk>/", views.WorkflowDetailView.as_view(), name="workflow_detail"),
    path("<int:pk>/edit/", views.WorkflowUpdateView.as_view(), name="workflow_update"),
    path("<int:pk>/delete/", views.WorkflowDeleteView.as_view(), name="workflow_delete"),
    path(
        "<int:pk>/validations/",
        views.WorkflowValidationListView.as_view(),
        name="workflow_validation_list",
    ),
]
