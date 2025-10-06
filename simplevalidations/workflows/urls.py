from django.urls import path

from simplevalidations.workflows import views

app_name = "workflows"

urlpatterns = [
    path("", views.WorkflowListView.as_view(), name="workflow_list"),
    path("new/", views.WorkflowCreateView.as_view(), name="workflow_create"),
    path("<int:pk>/", views.WorkflowDetailView.as_view(), name="workflow_detail"),
    path("<int:pk>/edit/", views.WorkflowUpdateView.as_view(), name="workflow_update"),
    path("<int:pk>/delete/", views.WorkflowDeleteView.as_view(), name="workflow_delete"),
    path(
        "<int:pk>/steps/",
        views.WorkflowStepListView.as_view(),
        name="workflow_step_list",
    ),
    path(
        "<int:pk>/steps/wizard/",
        views.WorkflowStepWizardView.as_view(),
        name="workflow_step_wizard",
    ),
    path(
        "<int:pk>/steps/<int:step_id>/wizard/",
        views.WorkflowStepWizardView.as_view(),
        name="workflow_step_wizard_existing",
    ),
    path(
        "<int:pk>/steps/<int:step_id>/delete/",
        views.WorkflowStepDeleteView.as_view(),
        name="workflow_step_delete",
    ),
    path(
        "<int:pk>/steps/<int:step_id>/move/",
        views.WorkflowStepMoveView.as_view(),
        name="workflow_step_move",
    ),
    path(
        "<int:pk>/validations/",
        views.WorkflowValidationListView.as_view(),
        name="workflow_validation_list",
    ),
]
