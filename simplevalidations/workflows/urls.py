from django.urls import path

from simplevalidations.workflows import views

app_name = "workflows"

urlpatterns = [
    path("", views.WorkflowListView.as_view(), name="workflow_list"),
    path("new/", views.WorkflowCreateView.as_view(), name="workflow_create"),
    path(
        "<int:pk>/launch/",
        views.WorkflowLaunchDetailView.as_view(),
        name="workflow_launch",
    ),
    path(
        "<int:pk>/launch/start/",
        views.WorkflowLaunchStartView.as_view(),
        name="workflow_launch_start",
    ),
    path(
        "<int:pk>/launch/run/<uuid:run_id>/status/",
        views.WorkflowLaunchStatusView.as_view(),
        name="workflow_launch_status",
    ),
    path(
        "<int:pk>/public-info/",
        views.WorkflowPublicInfoUpdateView.as_view(),
        name="workflow_public_info_edit",
    ),
    path(
        "<int:pk>/",
        views.WorkflowDetailView.as_view(),
        name="workflow_detail",
    ),
    path(
        "<int:pk>/activation/",
        views.WorkflowActivationUpdateView.as_view(),
        name="workflow_activation",
    ),
    path(
        "<int:pk>/edit/",
        views.WorkflowUpdateView.as_view(),
        name="workflow_update",
    ),
    path(
        "<int:pk>/delete/",
        views.WorkflowDeleteView.as_view(),
        name="workflow_delete",
    ),
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
        "<int:pk>/steps/add/<int:validator_id>/",
        views.WorkflowStepCreateView.as_view(),
        name="workflow_step_create",
    ),
    path(
        "<int:pk>/steps/<int:step_id>/wizard/",
        views.WorkflowStepWizardView.as_view(),
        name="workflow_step_wizard_existing",
    ),
    path(
        "<int:pk>/steps/<int:step_id>/edit/",
        views.WorkflowStepUpdateView.as_view(),
        name="workflow_step_edit",
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
