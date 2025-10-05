import base64
import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.urls import reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views.generic import DetailView
from django.views.generic import ListView
from django.views.generic import UpdateView
from django.views.generic.edit import CreateView
from django.views.generic.edit import DeleteView
from rest_framework import permissions
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from simplevalidations.core.mixins import BreadcrumbMixin
from simplevalidations.projects.models import Project
from simplevalidations.submissions.ingest import prepare_inline_text
from simplevalidations.submissions.ingest import prepare_uploaded_file
from simplevalidations.submissions.models import Submission
from simplevalidations.tracking.services import TrackingEventService
from simplevalidations.users.models import User
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.serializers import ValidationRunStartSerializer
from simplevalidations.validations.services.validation_run import ValidationRunService
from simplevalidations.workflows.constants import SUPPORTED_CONTENT_TYPES
from simplevalidations.workflows.forms import WorkflowForm
from simplevalidations.workflows.models import Workflow
from simplevalidations.workflows.request_utils import extract_request_basics
from simplevalidations.workflows.request_utils import is_raw_body_mode
from simplevalidations.workflows.serializers import WorkflowSerializer

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

        # TODO: When we support projects, we need to resolve the project here.
        # project = self._resolve_project(workflow=workflow, request=request)
        project = None  # We don't support projects yet

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
                project=project,
            )

        return self._handle_envelope_or_multipart_mode(
            request=request,
            workflow=workflow,
            user=user,
            project=project,
        )

    # ---------------------- Raw Body Mode ----------------------

    def _handle_raw_body_mode(
        self,
        request: Request,
        workflow: Workflow,
        user: User,
        content_type_header: str,
        body_bytes: bytes,
        project: Project | None,
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
            project=project,
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
        project: Project | None,
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
                project=project,
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
                project=project,
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
        response = service.launch(
            request=request,
            org=workflow.org,
            workflow=workflow,
            submission=submission,
            metadata=metadata,
            user_id=user_id,
        )

        response_data = getattr(response, "data", None)
        run_id = None
        run_status = None
        if isinstance(response_data, dict):
            run_id = response_data.get("id")
            run_status = response_data.get("status")

        extra_payload: dict[str, object] = {}
        if run_status is not None:
            extra_payload["validation_run_status"] = run_status
        status_code = getattr(response, "status_code", None)
        if status_code is not None:
            extra_payload["response_status_code"] = status_code
        if metadata:
            extra_payload["metadata_keys"] = sorted(metadata.keys())

        tracking_service = TrackingEventService()
        actor = (
            request.user
            if getattr(request, "user", None)
            and getattr(request.user, "is_authenticated", False)
            else None
        )
        tracking_service.log_validation_run_started(
            workflow=workflow,
            project=submission.project,
            user=actor,
            submission_id=submission.pk,
            validation_run_id=run_id,
            extra_data=extra_payload or None,
        )

        return response

    # Public action remains unchanged
    @action(
        detail=True,
        methods=["post"],
        url_path="start",
        url_name="start",
    )
    def start_validation(self, request, pk=None, *args, **kwargs):
        workflow = self.get_object()
        return self._start_validation_run_for_workflow(request, workflow)


# UI Views
# ------------------------------------------------------------------------------


class WorkflowAccessMixin(LoginRequiredMixin, BreadcrumbMixin):
    """Reusable helpers for workflow UI views."""

    def get_workflow_queryset(self):
        user = self.request.user
        return (
            Workflow.objects.for_user(user)
            .select_related("org", "user")
            .prefetch_related("runs")
            .order_by("name", "-version")
        )

    def get_queryset(self):
        return self.get_workflow_queryset()


class WorkflowListView(WorkflowAccessMixin, ListView):
    template_name = "workflows/workflow_list.html"
    context_object_name = "workflows"
    breadcrumbs = [
        {"name": _("Workflows"), "url": ""},
    ]

    def get_queryset(self):
        qs = super().get_queryset()
        search = self.request.GET.get("q", "").strip()
        if search:
            qs = qs.filter(name__icontains=search)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "search_query": self.request.GET.get("q", ""),
                "create_url": reverse("workflows:workflow_create"),
            },
        )
        return context


class WorkflowDetailView(WorkflowAccessMixin, DetailView):
    template_name = "workflows/workflow_detail.html"
    context_object_name = "workflow"

    def get_breadcrumbs(self):
        workflow = getattr(self, "object", None) or self.get_object()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {"name": _("Workflows"), "url": reverse("workflows:workflow_list")},
        )
        breadcrumbs.append({"name": workflow.name, "url": ""})
        return breadcrumbs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = context["workflow"]
        recent_runs = workflow.validation_runs.all().order_by("-created")[:5]
        context.update(
            {
                "related_validations_url": reverse(
                    "workflows:workflow_validation_list",
                    kwargs={"pk": workflow.pk},
                ),
                "recent_runs": recent_runs,
            },
        )
        return context


class WorkflowFormViewMixin(WorkflowAccessMixin):
    form_class = WorkflowForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs


class WorkflowCreateView(WorkflowFormViewMixin, CreateView):
    template_name = "workflows/workflow_form.html"
    breadcrumbs = [
        {"name": _("Workflows"), "url": reverse_lazy("workflows:workflow_list")},
        {"name": _("New Workflow"), "url": ""},
    ]

    def form_valid(self, form):
        user = self.request.user
        org = user.get_current_org()
        if org is None:
            form.add_error(
                None, _("You need an organization before creating workflows.")
            )
            return self.form_invalid(form)
        form.instance.org = org
        form.instance.user = user
        messages.success(self.request, _("Workflow created."))
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("workflows:workflow_detail", args=[self.object.pk])


class WorkflowUpdateView(WorkflowFormViewMixin, UpdateView):
    template_name = "workflows/workflow_form.html"

    def get_breadcrumbs(self):
        workflow = getattr(self, "object", None) or self.get_object()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {"name": _("Workflows"), "url": reverse("workflows:workflow_list")},
        )
        breadcrumbs.append(
            {
                "name": workflow.name,
                "url": reverse("workflows:workflow_detail", args=[workflow.pk]),
            },
        )
        breadcrumbs.append({"name": _("Edit"), "url": ""})
        return breadcrumbs

    def form_valid(self, form):
        messages.success(self.request, _("Workflow updated."))
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("workflows:workflow_detail", args=[self.object.pk])


class WorkflowDeleteView(WorkflowAccessMixin, DeleteView):
    template_name = "workflows/partials/workflow_confirm_delete.html"
    success_url = reverse_lazy("workflows:workflow_list")

    def get_breadcrumbs(self):
        workflow = getattr(self, "object", None) or self.get_object()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {"name": _("Workflows"), "url": reverse("workflows:workflow_list")},
        )
        breadcrumbs.append(
            {
                "name": workflow.name,
                "url": reverse("workflows:workflow_detail", args=[workflow.pk]),
            },
        )
        breadcrumbs.append({"name": _("Delete"), "url": ""})
        return breadcrumbs

    def post(self, request, *args, **kwargs):
        # Support HTMX POST fallback
        return self.delete(request, *args, **kwargs)

    def delete(self, request, *args, **kwargs):
        self.object = self.get_object()
        success_url = self.get_success_url()
        self.object.delete()
        messages.success(request, _("Workflow deleted."))
        if request.headers.get("HX-Request"):
            target = request.headers.get("HX-Target", "")
            response = HttpResponse("")
            response["HX-Trigger"] = "workflowDeleted"
            if target.startswith("workflow-card-wrapper-"):
                return response
            response["HX-Redirect"] = success_url
            return response
        if request.method == "DELETE":
            return HttpResponse(status=204)
        return HttpResponseRedirect(success_url)


class WorkflowValidationListView(WorkflowAccessMixin, ListView):
    template_name = "validations/workflow_validation_list.html"
    context_object_name = "validations"

    def get_workflow(self):
        if not hasattr(self, "_workflow"):
            self._workflow = get_object_or_404(
                self.get_workflow_queryset(),
                pk=self.kwargs.get("pk"),
            )
        return self._workflow

    def get_queryset(self):
        workflow = self.get_workflow()
        return (
            ValidationRun.objects.filter(workflow=workflow)
            .select_related("workflow", "submission", "org")
            .order_by("-created")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = self.get_workflow()
        context.update({"workflow": workflow})
        return context

    def get_breadcrumbs(self):
        workflow = self.get_workflow()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {"name": _("Workflows"), "url": reverse("workflows:workflow_list")},
        )
        breadcrumbs.append(
            {
                "name": workflow.name,
                "url": reverse("workflows:workflow_detail", args=[workflow.pk]),
            },
        )
        breadcrumbs.append({"name": _("Validations"), "url": ""})
        return breadcrumbs
