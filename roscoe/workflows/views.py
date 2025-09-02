from rest_framework import permissions
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from roscoe.users.constants import RoleCode
from roscoe.validations.serializers import ValidationRunStartSerializer
from roscoe.validations.services.launcher import ValidationJobLauncher
from roscoe.workflows.models import Workflow
from roscoe.workflows.serializers import WorkflowSerializer

# API Views
# ------------------------------------------------------------------------------


class WorkflowViewSet(viewsets.ModelViewSet):
    queryset = Workflow.objects.all()
    serializer_class = WorkflowSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # List all workflows the user can access (in any of their orgs)
        return Workflow.objects.for_user(self.request.user)

    def get_serializer_class(self):
        # Use a dedicated serializer for start/validate actions
        if getattr(self, "action", None) in ("start_validation", "validate_shortcut"):
            return ValidationRunStartSerializer
        return super().get_serializer_class()

    def _start_run_for_workflow(self, request, workflow: Workflow):
        user = request.user

        # Require that the user can access AND has the EXECUTOR role in the
        # workflow's org
        can_execute = (
            Workflow.objects.for_user(user, required_role_code=RoleCode.EXECUTE)
            .filter(pk=workflow.pk)
            .exists()
        )
        if not can_execute:
            return Response(
                {"detail": "Workflow not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Validate incoming payload with ValidationRunStartSerializer
        payload = request.data.copy()
        payload["workflow"] = workflow.pk
        serializer = self.get_serializer(
            data=payload,
        )  # uses ValidationRunStartSerializer
        serializer.is_valid(raise_exception=True)

        document = serializer.validated_data["document"]
        metadata = serializer.validated_data.get("metadata", {})

        launcher = ValidationJobLauncher()
        return launcher.launch(
            request=request,
            org=workflow.org,
            workflow=workflow,
            submission=None,  # no Submission path for now
            document=document,
            metadata=metadata,
            user_id=getattr(user, "id", None),
        )

    # A user can start a validation run for a workflow
    # using either of these two endpoints:
    # /workflows/{id}/start/ or /workflows/{id}/validate/
    # Both endpoints do the same thing: start the validation run.

    @action(detail=True, methods=["post"], url_path="start")
    def start_validation(self, request, pk=None):
        workflow = self.get_object()
        return self._start_run_for_workflow(request, workflow)


# Template Views
# ------------------------------------------------------------------------------

# TODO ...
