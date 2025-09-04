import base64

from django.core.files.base import ContentFile
from django.utils.translation import gettext_lazy as _
from rest_framework import permissions
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from roscoe.submissions.models import Submission
from roscoe.validations.serializers import ValidationRunStartSerializer
from roscoe.validations.services.validation_run import ValidationRunService
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
        if getattr(self, "action", None) in [
            "start_validation",  # may add others later
        ]:
            return ValidationRunStartSerializer
        return super().get_serializer_class()

    def _start_validation_run_for_workflow(
        self,
        request,
        workflow: Workflow,
    ):
        user = request.user

        # Require that the user can access AND has the EXECUTOR role in the
        # workflow's org
        if not workflow.can_execute(user=user):
            return Response(
                {"detail": _("Workflow not found.")},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Validate incoming payload with ValidationRunStartSerializer
        payload = request.data.copy()
        payload["workflow"] = workflow.pk
        serializer = self.get_serializer(
            data=payload,
        )  # uses ValidationRunStartSerializer
        serializer.is_valid(raise_exception=True)
        vd = serializer.validated_data

        # Normalize metadata
        metadata = vd.get("metadata") or {}

        # Build the Submission
        submission = Submission(
            org=workflow.org,
            workflow=workflow,
            user=getattr(user, "pk", None) and user,
            project=None,  # or project from context if applicable
            name=vd.get("filename", "") or "",
            metadata=metadata,
        )

        # Choose exactly one content path
        filename = vd.get("filename") or ""
        file_type_hint = vd.get("file_type")
        inline_text = vd.get("document")
        uploaded_file = vd.get("file")
        content_b64 = vd.get("content_b64")
        upload_id = vd.get("upload_id")

        if uploaded_file is not None:
            submission.set_content(
                uploaded_file=uploaded_file,
                filename=filename,
                file_type=file_type_hint,
            )
        elif inline_text is not None:
            submission.set_content(
                inline_text=inline_text,
                filename=filename,
                file_type=file_type_hint,
            )
        elif content_b64 is not None:
            raw = base64.b64decode(content_b64)
            # Use ContentFile to hand bytes to set_content as if it were an upload
            cf = ContentFile(raw, name=filename or "upload")
            submission.set_content(
                uploaded_file=cf,
                filename=filename,
                file_type=file_type_hint,
            )
        elif upload_id is not None:
            # Optional: resolve a pre-uploaded file by reference
            file_like = self._resolve_upload(upload_id)
            if not file_like:
                return Response(
                    {"detail": _("upload_id not found or expired.")},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            submission.set_content(
                uploaded_file=file_like, filename=filename, file_type=file_type_hint
            )
        else:
            # Shouldn't happen due to serializer, but just in case
            return Response(
                {"detail": _("No content provided.")},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        # Validate + persist Submission
        submission.full_clean()
        submission.save()

        service = ValidationRunService()
        return service.launch(
            request=request,
            org=workflow.org,
            workflow=workflow,
            submission=submission,
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
        return self._start_validation_run_for_workflow(request, workflow)

    def _resolve_upload(self, upload_id: str):
        """
        OPTIONAL: implement if you support by-reference uploads.
        For example, look up an Upload model and return a File-like object.
        Raise an exception or return None if not found/expired.
        """
        # TODO: implement
        return None  # lets the caller return 422 cleanly


# Template Views
# ------------------------------------------------------------------------------

# TODO ...
