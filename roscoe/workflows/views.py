import base64
import logging

from django.conf import settings
from django.db import transaction
from django.utils.translation import gettext_lazy as _
from rest_framework import permissions
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from roscoe.submissions.ingest import prepare_inline_text
from roscoe.submissions.ingest import prepare_uploaded_file
from roscoe.submissions.models import Submission
from roscoe.users.models import User
from roscoe.validations.serializers import ValidationRunStartSerializer
from roscoe.validations.services.validation_run import ValidationRunService
from roscoe.workflows.constants import SUPPORTED_CONTENT_TYPES
from roscoe.workflows.models import Workflow
from roscoe.workflows.request_utils import extract_request_basics
from roscoe.workflows.request_utils import is_raw_body_mode
from roscoe.workflows.serializers import WorkflowSerializer

logger = logging.getLogger(__name__)


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
        if getattr(self, "action", None) in [
            "start_validation",
        ]:
            return ValidationRunStartSerializer
        return super().get_serializer_class()

    def _start_validation_run_for_workflow(
        self,
        request,
        workflow: Workflow,
    ) -> Response:
        """
        Helper to start a validation run for a given workflow.

        Args:
            request (Request):
            workflow (Workflow):

        Returns:
            Response
        """
        user = request.user

        if not workflow.can_execute(user=user):
            return Response(
                {"detail": _("Workflow not found.")},
                status=status.HTTP_404_NOT_FOUND,
            )

        content_type_header, body_bytes = extract_request_basics(request)

        if is_raw_body_mode(request, content_type_header, body_bytes):
            return self._handle_raw_body_mode(
                request=request,
                workflow=workflow,
                user=user,
                content_type_header=content_type_header,
                body_bytes=body_bytes,
            )

        return self._handle_envelope_or_multipart_mode(
            request=request,
            workflow=workflow,
            user=user,
        )

    # ---------------------- Raw Body Mode ----------------------

    def _handle_raw_body_mode(
        self,
        request: Request,
        workflow: Workflow,
        user: User,
        content_type_header: str,
        body_bytes: bytes,
    ) -> Response:
        """
        Process Mode 1 (raw body) submission.
        """
        encoding = request.headers.get("Content-Encoding")
        # keep full header for charset extraction
        full_ct = request.headers.get("Content-Type", "")
        filename = request.headers.get("X-Filename") or ""
        raw = body_bytes
        max_inline = getattr(settings, "SUBMISSION_INLINE_MAX_BYTES", 10_000_000)
        if len(raw) > max_inline:
            return Response(
                {
                    "detail": _("Payload too large."),  # noqa:F823
                },
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        if encoding:
            if encoding.lower() != "base64":
                return Response(
                    {"detail": _("Unsupported Content-Encoding (only base64).")},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                raw = base64.b64decode(raw, validate=True)
            except Exception:
                return Response(
                    {"detail": _("Invalid base64 payload.")},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # normalize content-type (no params) and map to file_type
        ct_norm = (content_type_header or "").split(";")[0].strip().lower()
        file_type = SUPPORTED_CONTENT_TYPES.get(ct_norm)
        if not file_type:
            err_msg = _("Unsupported Content-Type '%s'. ") % full_ct
            err_msg += _("Supported : %s") % ", ".join(SUPPORTED_CONTENT_TYPES.keys())
            return Response(
                {
                    "detail": err_msg,
                },
                status=400,
            )

        # try charset from full header first
        charset = "utf-8"
        if ";" in full_ct:
            for part in full_ct.split(";")[1:]:
                k, _, v = part.partition("=")
                if k.strip().lower() == "charset" and v.strip():
                    charset = v.strip()
                    break
        try:
            text_content = raw.decode(charset)
        except UnicodeDecodeError:
            # last resort
            text_content = raw.decode("utf-8", errors="replace")

        safe_filename, ingest = prepare_inline_text(
            text=text_content,
            filename=filename,
            content_type=ct_norm,  # use normalized CT
            deny_magic_on_text=True,
        )

        submission = Submission(
            org=workflow.org,
            workflow=workflow,
            user=user,
            project=None,
            name=safe_filename,
            checksum_sha256=ingest.sha256,
            metadata={},
        )
        submission.set_content(
            inline_text=text_content,
            filename=safe_filename,
            file_type=file_type,
        )

        with transaction.atomic():
            submission.full_clean()
            submission.save()
            return self._launch_validation_run(
                request=request,
                workflow=workflow,
                submission=submission,
                metadata={},
                user_id=getattr(user, "id", None),
            )

    # ---------------------- Envelope / Multipart Modes ----------------------

    def _handle_envelope_or_multipart_mode(
        self,
        request: Request,
        workflow: Workflow,
        user: User,
    ) -> Response:
        """
        Process Mode 2 (JSON envelope) or Mode 3 (multipart).
        """
        payload = request.data.copy()
        payload["workflow"] = workflow.pk
        serializer = self.get_serializer(data=payload)
        serializer.is_valid(raise_exception=True)

        vd = serializer.validated_data
        file_obj = vd.get("file", None)
        if file_obj is not None:
            max_file = getattr(settings, "SUBMISSION_FILE_MAX_BYTES", 1_000_000_000)
            if getattr(file_obj, "size", 0) > max_file:
                return Response({"detail": _("File too large.")}, status=413)

        metadata = vd.get("metadata") or {}

        if vd.get("file") is not None:
            file_obj = vd["file"]
            ct = vd["content_type"]  # already normalized by serializer
            max_file = getattr(settings, "SUBMISSION_FILE_MAX_BYTES", 1_000_000_000)
            ingest = prepare_uploaded_file(
                uploaded_file=file_obj,
                filename=vd.get("filename") or getattr(file_obj, "name", "") or "",
                content_type=ct,
                max_bytes=max_file,
            )
            safe_filename = ingest.filename

            submission = Submission(
                org=workflow.org,
                workflow=workflow,
                user=user if getattr(user, "is_authenticated", False) else None,
                project=None,
                name=safe_filename,
                metadata={},
                checksum_sha256=ingest.sha256,
            )
            submission.set_content(
                uploaded_file=file_obj,
                filename=safe_filename,
                file_type=vd["file_type"],
            )

        elif vd.get("normalized_content") is not None:
            ct = vd["content_type"]
            safe_filename, ingest = prepare_inline_text(
                text=vd["normalized_content"],
                filename=vd.get("filename") or "",
                content_type=ct,
                deny_magic_on_text=True,
            )
            metadata = {**metadata, "sha256": ingest.sha256}

            submission = Submission(
                org=workflow.org,
                workflow=workflow,
                user=user if getattr(user, "is_authenticated", False) else None,
                project=None,
                name=safe_filename,
                metadata=metadata,
            )
            submission.set_content(
                inline_text=vd["normalized_content"],
                filename=safe_filename,
                file_type=vd["file_type"],
            )
        else:
            return Response(
                {"detail": _("No content provided.")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        with transaction.atomic():
            submission.full_clean()
            submission.save()
            return self._launch_validation_run(
                request=request,
                workflow=workflow,
                submission=submission,
                metadata=metadata,
                user_id=getattr(user, "id", None),
            )

    # ---------------------- Launch Helper ----------------------

    def _launch_validation_run(
        self,
        request: Request,
        workflow: Workflow,
        submission: Submission,
        metadata: dict,
        user_id: int | None = None,
    ) -> Response:
        """
        Thin wrapper over ValidationRunService.launch to centralize call site.
        """
        service = ValidationRunService()
        return service.launch(
            request=request,
            org=workflow.org,
            workflow=workflow,
            submission=submission,
            metadata=metadata,
            user_id=user_id,
        )

    # Public action remains unchanged
    @action(
        detail=True,
        methods=["post"],
        url_path="start",
    )
    def start_validation(self, request, pk=None, *args, **kwargs):
        workflow = self.get_object()
        return self._start_validation_run_for_workflow(request, workflow)
