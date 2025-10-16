import base64
import json
import logging
import uuid
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import models
from django.db import transaction
from django.http import Http404
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.urls import reverse
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import DetailView
from django.views.generic import ListView
from django.views.generic import TemplateView
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
from simplevalidations.core.utils import reverse_with_org
from simplevalidations.projects.models import Project
from simplevalidations.submissions.ingest import prepare_inline_text
from simplevalidations.submissions.ingest import prepare_uploaded_file
from simplevalidations.submissions.models import Submission
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.models import User
from simplevalidations.validations.constants import RulesetType
from simplevalidations.validations.constants import StepStatus
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.constants import XMLSchemaType
from simplevalidations.validations.models import Ruleset
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.models import Validator
from simplevalidations.validations.serializers import ValidationRunStartSerializer
from simplevalidations.validations.services.validation_run import ValidationRunService
from simplevalidations.workflows.constants import SUPPORTED_CONTENT_TYPES
from simplevalidations.workflows.forms import AiAssistStepConfigForm
from simplevalidations.workflows.forms import EnergyPlusStepConfigForm
from simplevalidations.workflows.forms import JsonSchemaStepConfigForm
from simplevalidations.workflows.forms import WorkflowForm
from simplevalidations.workflows.forms import WorkflowLaunchForm
from simplevalidations.workflows.forms import WorkflowPublicInfoForm
from simplevalidations.workflows.forms import WorkflowStepTypeForm
from simplevalidations.workflows.forms import XmlSchemaStepConfigForm
from simplevalidations.workflows.forms import get_config_form_class
from simplevalidations.workflows.models import Workflow
from simplevalidations.workflows.models import WorkflowStep
from simplevalidations.workflows.request_utils import extract_request_basics
from simplevalidations.workflows.request_utils import is_raw_body_mode
from simplevalidations.workflows.serializers import WorkflowSerializer

logger = logging.getLogger(__name__)

MAX_STEP_COUNT = 5


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

        project = self._resolve_project(workflow=workflow, request=request)

        if not workflow.is_active:
            return Response(
                {"detail": _("This workflow is inactive and cannot accept runs.")},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not workflow.can_execute(user=user):
            # Return 404 to avoid leaking workflow existence when user lacks access.
            raise Http404

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
        try:
            serializer.is_valid(raise_exception=True)
        except Exception as e:
            logger.info(
                "ValidationRunStartSerializer invalid: %s",
                getattr(e, "detail", str(e)),
            )
            raise e  # noqa: TRY201

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

        return response

    def _resolve_project(self, workflow: Workflow, request: Request) -> Project | None:
        project_id = request.query_params.get("project") or request.GET.get("project")
        if project_id:
            try:
                return Project.objects.get(pk=project_id, org=workflow.org)
            except Project.DoesNotExist as exc:  # pragma: no cover
                raise Http404 from exc
        return workflow.project

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

    manager_role_codes = {
        RoleCode.OWNER,
        RoleCode.ADMIN,
        RoleCode.AUTHOR,
    }

    def get_workflow_queryset(self):
        user = self.request.user
        return (
            Workflow.objects.for_user(user)
            .select_related("org", "user", "project")
            .prefetch_related("validation_runs")
            .order_by("name", "-version")
        )

    def get_queryset(self):
        return self.get_workflow_queryset()

    def user_can_manage_workflow(self, *, user: User | None = None) -> bool:
        user = user or self.request.user
        if not getattr(user, "is_authenticated", False):
            return False
        membership = user.membership_for_current_org()
        if membership is None or not membership.is_active:
            return False
        return any(membership.has_role(code) for code in self.manager_role_codes)


class WorkflowObjectMixin(WorkflowAccessMixin):
    workflow_url_kwarg = "pk"

    def get_workflow(self) -> Workflow:
        if not hasattr(self, "_workflow"):
            queryset = (
                self.get_workflow_queryset()
                .select_related("org", "user", "project")
                .prefetch_related("steps")
            )
            workflow_id = self.kwargs.get(self.workflow_url_kwarg)
            self._workflow = get_object_or_404(queryset, pk=workflow_id)
        return self._workflow


def _resequence_workflow_steps(workflow: Workflow) -> None:
    ordered = list(workflow.steps.all().order_by("order", "pk"))
    changed = False
    for index, step in enumerate(ordered, start=1):
        desired = index * 10
        if step.order != desired:
            step.order = desired
            changed = True
    if changed:
        WorkflowStep.objects.bulk_update(ordered, ["order"])


def _hx_trigger_response(
    message: str | None = None,
    level: str = "success",
    *,
    status_code: int = 204,
    close_modal: str | None = "workflowStepModal",
) -> HttpResponse:
    response = HttpResponse(status=status_code)
    payload: dict[str, object] = {"steps-changed": True}
    if message:
        payload["toast"] = {"level": level, "message": str(message)}
    if close_modal:
        payload["close-modal"] = close_modal
    response["HX-Trigger"] = json.dumps(payload)
    return response


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
        workflows = list(context["workflows"])
        context["workflows"] = workflows
        context["object_list"] = workflows
        user = self.request.user
        for wf in workflows:
            wf.can_execute_cached = wf.can_execute(user=user)

        context.update(
            {
                "search_query": self.request.GET.get("q", ""),
                "create_url": reverse_with_org(
                    "workflows:workflow_create",
                    request=self.request,
                ),
            },
        )
        return context


class WorkflowDetailView(WorkflowAccessMixin, DetailView):
    template_name = "workflows/workflow_detail.html"
    context_object_name = "workflow"

    def get_queryset(self):
        return super().get_queryset().prefetch_related("steps")

    def get_breadcrumbs(self):
        workflow = getattr(self, "object", None) or self.get_object()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Workflows"),
                "url": reverse_with_org(
                    "workflows:workflow_list",
                    request=self.request,
                ),
            },
        )
        breadcrumbs.append({"name": workflow.name, "url": ""})
        return breadcrumbs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = context["workflow"]
        recent_runs = workflow.validation_runs.all().order_by("-created")[:5]
        context.update(
            {
                "related_validations_url": reverse_with_org(
                    "workflows:workflow_validation_list",
                    request=self.request,
                    kwargs={"pk": workflow.pk},
                ),
                "recent_runs": recent_runs,
                "max_step_count": MAX_STEP_COUNT,
                "can_manage_activation": self.user_can_manage_workflow(),
                "public_info_url": (
                    self.request.build_absolute_uri(
                        reverse(
                            "workflow_public_info",
                            kwargs={"workflow_uuid": workflow.uuid},
                        ),
                    )
                    if workflow.make_info_public
                    else None
                ),
            },
        )
        return context


class WorkflowLaunchContextMixin(WorkflowObjectMixin):
    launch_panel_template_name = "workflows/launch/_launch_panel.html"
    run_status_template_name = "workflows/launch/_run_status.html"
    polling_statuses = {
        ValidationRunStatus.PENDING,
        ValidationRunStatus.RUNNING,
    }

    def get_recent_runs(self, workflow: Workflow, limit: int = 5):
        return list(
            ValidationRun.objects.filter(workflow=workflow)
            .select_related("submission", "user")
            .order_by("-created")[:limit],
        )

    def get_launch_form(
        self,
        *,
        workflow: Workflow,
        data=None,
        files=None,
    ) -> WorkflowLaunchForm:
        return WorkflowLaunchForm(
            data=data,
            files=files,
            workflow=workflow,
            user=self.request.user,
        )

    def load_run_for_display(
        self,
        *,
        workflow: Workflow,
        run_id,
    ) -> ValidationRun | None:
        if not run_id:
            return None
        try:
            uuid_val = (
                run_id if isinstance(run_id, uuid.UUID) else uuid.UUID(str(run_id))
            )
        except (TypeError, ValueError):
            return None
        return (
            ValidationRun.objects.filter(pk=uuid_val, workflow=workflow)
            .select_related("submission", "user")
            .prefetch_related("step_runs", "step_runs__workflow_step", "findings")
            .first()
        )

    def build_launch_context(
        self,
        *,
        workflow: Workflow,
        form: WorkflowLaunchForm,
        active_run: ValidationRun | None,
    ) -> dict[str, object]:
        if active_run:
            step_runs = list(active_run.step_runs.order_by("step_order"))
            findings = list(active_run.findings.order_by("severity", "-created")[:10])
            polling = active_run.status in self.polling_statuses
        else:
            step_runs = []
            findings = []
            polling = False
        return {
            "workflow": workflow,
            "launch_form": form,
            "can_execute": workflow.can_execute(user=self.request.user),
            "recent_runs": self.get_recent_runs(workflow),
            "active_run": active_run,
            "active_run_step_runs": step_runs,
            "active_run_findings": findings,
            "active_run_is_polling": polling,
            "polling_statuses": self.polling_statuses,
        }


class WorkflowLaunchDetailView(WorkflowLaunchContextMixin, TemplateView):
    template_name = "workflows/launch/workflow_launch.html"

    def get_breadcrumbs(self):
        workflow = self.get_workflow()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Workflows"),
                "url": reverse_with_org(
                    "workflows:workflow_list",
                    request=self.request,
                ),
            },
        )
        breadcrumbs.append(
            {
                "name": workflow.name,
                "url": reverse_with_org(
                    "workflows:workflow_detail",
                    request=self.request,
                    kwargs={"pk": workflow.pk},
                ),
            },
        )
        breadcrumbs.append({"name": _("Run"), "url": ""})
        return breadcrumbs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = self.get_workflow()
        can_execute = workflow.can_execute(user=self.request.user)
        form = self.get_launch_form(workflow=workflow) if can_execute else None
        context.update(
            {
                "workflow": workflow,
                "recent_runs": self.get_recent_runs(workflow),
                "can_execute": can_execute,
                "launch_form": form,
                "active_run": None,
                "active_run_step_runs": [],
                "active_run_findings": [],
                "active_run_is_polling": False,
                "polling_statuses": self.polling_statuses,
            },
        )
        return context


class WorkflowLaunchStartView(WorkflowLaunchContextMixin, View):
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        can_execute = workflow.can_execute(user=request.user)
        if not can_execute:
            return self._launch_response(
                request,
                workflow=workflow,
                form=None,
                active_run=None,
                status_code=403,
                toast={
                    "level": "danger",
                    "message": str(_("You cannot run this workflow.")),
                },
            )

        form = self.get_launch_form(
            workflow=workflow,
            data=request.POST,
            files=request.FILES,
        )

        if not form.is_valid():
            return self._launch_response(
                request,
                workflow=workflow,
                form=form,
                active_run=None,
                status_code=400,
            )

        try:
            submission = self._create_submission(
                request=request,
                workflow=workflow,
                form=form,
            )
        except ValidationError as exc:
            form.add_error(None, exc.message if hasattr(exc, "message") else str(exc))
            return self._launch_response(
                request,
                workflow=workflow,
                form=form,
                active_run=None,
                status_code=400,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "Failed to prepare submission for workflow run.", exc_info=exc
            )
            form.add_error(
                None, _("Something went wrong while preparing the submission.")
            )
            return self._launch_response(
                request,
                workflow=workflow,
                form=form,
                active_run=None,
                status_code=500,
            )

        try:
            service = ValidationRunService()
            response = service.launch(
                request,
                org=workflow.org,
                workflow=workflow,
                submission=submission,
                user_id=getattr(request.user, "id", None),
                metadata=form.cleaned_data.get("metadata"),
            )
        except PermissionError as exc:
            logger.info("Permission denied running workflow %s: %s", workflow.pk, exc)
            form.add_error(
                None, _("You do not have permission to run this workflow.")
            )
            return self._launch_response(
                request,
                workflow=workflow,
                form=form,
                active_run=None,
                status_code=403,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "Run service errored for workflow %s", workflow.pk, exc_info=exc
            )
            form.add_error(None, _("Could not run the workflow. Please try again."))
            return self._launch_response(
                request,
                workflow=workflow,
                form=form,
                active_run=None,
                status_code=500,
            )

        run_id = None
        response_data = getattr(response, "data", None)
        if isinstance(response_data, dict):
            run_id = response_data.get("id")

        active_run = self.load_run_for_display(workflow=workflow, run_id=run_id)

        toast_payload = {
            "level": "success",
            "message": str(_("Validation run started.")),
        }

        return self._launch_response(
            request,
            workflow=workflow,
            form=self.get_launch_form(workflow=workflow),
            active_run=active_run,
            status_code=getattr(response, "status_code", 200),
            toast=toast_payload,
        )

    def _create_submission(
        self,
        *,
        request,
        workflow: Workflow,
        form: WorkflowLaunchForm,
    ) -> Submission:
        cleaned = form.cleaned_data
        payload = cleaned.get("payload")
        attachment = cleaned.get("attachment")
        content_type = cleaned["content_type"]
        filename = cleaned.get("filename") or ""
        metadata = cleaned.get("metadata") or {}
        file_type = SUPPORTED_CONTENT_TYPES[content_type]

        submission = Submission(
            org=workflow.org,
            workflow=workflow,
            user=request.user
            if getattr(request.user, "is_authenticated", False)
            else None,
            project=workflow.project,
            name="",
            metadata=metadata,
            checksum_sha256="",
        )

        if attachment:
            max_file = int(
                getattr(settings, "SUBMISSION_FILE_MAX_BYTES", 1_000_000_000)
            )
            ingest = prepare_uploaded_file(
                uploaded_file=attachment,
                filename=filename,
                content_type=content_type,
                max_bytes=max_file,
            )
            safe_filename = ingest.filename
            submission.name = safe_filename
            submission.checksum_sha256 = ingest.sha256
            submission.set_content(
                uploaded_file=attachment,
                filename=safe_filename,
                file_type=file_type,
            )
        else:
            safe_filename, ingest = prepare_inline_text(
                text=payload,
                filename=filename,
                content_type=content_type,
                deny_magic_on_text=True,
            )
            submission.name = safe_filename
            submission.checksum_sha256 = ingest.sha256
            submission.set_content(
                inline_text=payload,
                filename=safe_filename,
                file_type=file_type,
            )

        with transaction.atomic():
            submission.full_clean()
            submission.save()
        return submission

    def _launch_response(
        self,
        request,
        *,
        workflow: Workflow,
        form: WorkflowLaunchForm | None,
        active_run: ValidationRun | None,
        status_code: int,
        toast: dict[str, str] | None = None,
    ):
        form = form or self.get_launch_form(workflow=workflow)
        context = self.build_launch_context(
            workflow=workflow,
            form=form,
            active_run=active_run,
        )
        template_name = self.launch_panel_template_name
        if request.headers.get("HX-Request") == "true":
            response = render(
                request,
                template_name,
                context=context,
                status=status_code,
            )
        else:
            response = render(
                request,
                "workflows/launch/workflow_launch.html",
                context=context,
                status=status_code,
            )
        if toast:
            sanitized_toast = {
                key: str(value) if isinstance(value, Promise) else value
                for key, value in toast.items()
            }
            response["HX-Trigger"] = json.dumps({"toast": sanitized_toast})
        return response


class WorkflowLaunchStatusView(WorkflowLaunchContextMixin, View):
    http_method_names = ["get"]

    def get(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        run_id = kwargs.get("run_id")
        run = self.load_run_for_display(workflow=workflow, run_id=run_id)
        if run is None:
            raise Http404

        context = {
            "workflow": workflow,
            "run": run,
            "step_runs": run.step_runs.order_by("step_order"),
            "findings": run.findings.order_by("severity", "-created")[:10],
            "is_polling": run.status in self.polling_statuses,
            "polling_statuses": self.polling_statuses,
            "status_url": reverse_with_org(
                "workflows:workflow_launch_status",
                request=request,
                kwargs={"pk": workflow.pk, "run_id": run.pk},
            ),
            "detail_url": reverse_with_org(
                "validations:validation_detail",
                request=request,
                kwargs={"pk": run.pk},
            ),
        }
        return render(
            request,
            self.run_status_template_name,
            context=context,
        )


class WorkflowPublicInfoView(DetailView):
    template_name = "workflows/public/workflow_info.html"
    context_object_name = "workflow"
    slug_field = "uuid"
    slug_url_kwarg = "workflow_uuid"
    def get_queryset(self):
        return (
            Workflow.objects.filter(make_info_public=True)
            .select_related("org", "project", "user")
            .prefetch_related("steps")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = context["workflow"]
        user = self.request.user
        context.update(
            {
                "steps": workflow.steps.all().order_by("order"),
                "recent_runs": list(
                    workflow.validation_runs.select_related("user").order_by(
                        "-created"
                    )[:5],
                ),
                "user_has_access": (
                    user.is_authenticated and workflow.can_execute(user=user)
                ),
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

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Workflows"),
                "url": reverse_with_org(
                    "workflows:workflow_list",
                    request=self.request,
                ),
            },
        )
        breadcrumbs.append({"name": _("New Workflow"), "url": ""})
        return breadcrumbs

    def form_valid(self, form):
        user = self.request.user
        org = user.get_current_org()
        if org is None:
            form.add_error(
                None,
                _("You need an organization before creating workflows."),
            )
            return self.form_invalid(form)
        form.instance.org = org
        form.instance.user = user
        messages.success(self.request, _("Workflow created."))
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_with_org(
            "workflows:workflow_detail",
            request=self.request,
            kwargs={"pk": self.object.pk},
        )


class WorkflowUpdateView(WorkflowFormViewMixin, UpdateView):
    template_name = "workflows/workflow_form.html"

    def get_breadcrumbs(self):
        workflow = getattr(self, "object", None) or self.get_object()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Workflows"),
                "url": reverse_with_org(
                    "workflows:workflow_list",
                    request=self.request,
                ),
            },
        )
        breadcrumbs.append(
            {
                "name": workflow.name,
                "url": reverse_with_org(
                    "workflows:workflow_detail",
                    request=self.request,
                    kwargs={"pk": workflow.pk},
                ),
            },
        )
        breadcrumbs.append({"name": _("Edit"), "url": ""})
        return breadcrumbs

    def form_valid(self, form):
        messages.success(self.request, _("Workflow updated."))
        return super().form_valid(form)

    def get_success_url(self):
        return reverse_with_org(
            "workflows:workflow_detail",
            request=self.request,
            kwargs={"pk": self.object.pk},
        )


class WorkflowDeleteView(WorkflowAccessMixin, DeleteView):
    template_name = "workflows/partials/workflow_confirm_delete.html"

    def get_success_url(self):
        return reverse_with_org("workflows:workflow_list", request=self.request)

    def get_breadcrumbs(self):
        workflow = getattr(self, "object", None) or self.get_object()
        breadcrumbs = super().get_breadcrumbs()
        breadcrumbs.append(
            {
                "name": _("Workflows"),
                "url": reverse_with_org(
                    "workflows:workflow_list",
                    request=self.request,
                ),
            },
        )
        breadcrumbs.append(
            {
                "name": workflow.name,
                "url": reverse_with_org(
                    "workflows:workflow_detail",
                    request=self.request,
                    kwargs={"pk": workflow.pk},
                ),
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


class WorkflowPublicInfoUpdateView(WorkflowObjectMixin, UpdateView):
    template_name = "workflows/workflow_public_info_form.html"
    form_class = WorkflowPublicInfoForm

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            return HttpResponse(status=403)
        return super().dispatch(request, *args, **kwargs)

    def get_object(self, queryset=None):
        return self.get_workflow().get_public_info

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["workflow"] = self.get_workflow()
        return kwargs

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, _("Public workflow info updated."))
        return response

    def get_success_url(self):
        return reverse_with_org(
            "workflows:workflow_public_info_edit",
            request=self.request,
            kwargs={"pk": self.get_workflow().pk},
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
            {
                "name": _("Workflows"),
                "url": reverse_with_org(
                    "workflows:workflow_list",
                    request=self.request,
                ),
            },
        )
        breadcrumbs.append(
            {
                "name": workflow.name,
                "url": reverse_with_org(
                    "workflows:workflow_detail",
                    request=self.request,
                    kwargs={"pk": workflow.pk},
                ),
            },
        )
        breadcrumbs.append({"name": _("Public info"), "url": ""})
        return breadcrumbs


class WorkflowActivationUpdateView(WorkflowObjectMixin, View):
    """Toggle workflow availability."""

    def post(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        if not self.user_can_manage_workflow():
            return HttpResponse(status=403)

        raw_state = (request.POST.get("is_active") or "").strip().lower()
        if raw_state in {"true", "1", "on"}:
            new_state = True
        elif raw_state in {"false", "0", "off"}:
            new_state = False
        else:
            return HttpResponse(status=400)

        if workflow.is_active != new_state:
            workflow.is_active = new_state
            workflow.save(update_fields=["is_active"])
            if new_state:
                messages.success(
                    request,
                    _(
                        "Workflow reactivated. New validation runs can start immediately."
                    ),
                )
            else:
                messages.info(
                    request,
                    _(
                        "Workflow disabled. Existing runs finish, but new ones are blocked."
                    ),
                )
        else:
            messages.info(
                request,
                _("No change applied—the workflow is already in that state."),
            )

        redirect_url = reverse_with_org(
            "workflows:workflow_detail",
            request=request,
            kwargs={"pk": workflow.pk},
        )

        if request.headers.get("HX-Request"):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = redirect_url
            return response

        return HttpResponseRedirect(redirect_url)


class WorkflowStepListView(WorkflowObjectMixin, View):
    template_name = "workflows/partials/workflow_step_list.html"

    def get(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        steps = workflow.steps.all().order_by("order", "pk")
        for step in steps:
            config = step.config or {}
            if step.validator.validation_type == ValidationType.ENERGYPLUS:
                band = config.get("eui_band") or {}
                config.setdefault(
                    "eui_band",
                    {
                        "min": band.get("min"),
                        "max": band.get("max"),
                    },
                )
            elif step.validator.validation_type == ValidationType.XML_SCHEMA:
                schema_type = config.get("schema_type")
                if schema_type:
                    try:
                        config["schema_type_label"] = XMLSchemaType(schema_type).label
                    except ValueError:
                        config["schema_type_label"] = schema_type
            step.config = config
        context = {
            "workflow": workflow,
            "steps": steps,
            "max_step_count": MAX_STEP_COUNT,
        }
        return render(request, self.template_name, context)


class WorkflowStepWizardView(WorkflowObjectMixin, View):
    template_select = "workflows/partials/workflow_step_wizard_select.html"
    template_config = "workflows/partials/workflow_step_wizard_config.html"

    def dispatch(self, request, *args, **kwargs):
        if not request.headers.get("HX-Request"):
            return HttpResponse(status=400)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        step = self._get_step()
        if step is not None:
            validator = step.validator
            form = self._build_config_form(validator, step=step)
            return self._render_config(request, workflow, validator, form, step)
        if workflow.steps.count() >= MAX_STEP_COUNT:
            context = {
                "workflow": workflow,
                "form": None,
                "validators_by_type": [],
                "max_step_count": MAX_STEP_COUNT,
                "step": None,
                "limit_reached": True,
            }
            return render(request, self.template_select, context, status=409)
        return self._render_select(request, workflow)

    def post(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        step = self._get_step()
        stage = request.POST.get("stage", "select")

        if stage == "select" and step is None:
            validators = self._available_validators(workflow)
            form = WorkflowStepTypeForm(request.POST, validators=validators)
            if form.is_valid():
                validator = form.get_validator()
                config_form = self._build_config_form(validator, step=None)
                return self._render_config(
                    request,
                    workflow,
                    validator,
                    config_form,
                    None,
                )
            return self._render_select(request, workflow, form=form)

        validator = self._resolve_validator(request, step)
        if validator is None:
            return HttpResponse(status=400)

        config_form = self._build_config_form(
            validator,
            step=step,
            data=request.POST,
            files=request.FILES,
        )
        if config_form.is_valid():
            if step is None and workflow.steps.count() >= MAX_STEP_COUNT:
                message = _("You can add up to %(count)s steps per workflow.") % {
                    "count": MAX_STEP_COUNT,
                }
                return _hx_trigger_response(message, level="warning", status_code=409)
            self._save_step(workflow, validator, config_form, step=step)
            _resequence_workflow_steps(workflow)
            message = _("Workflow step saved.")
            return _hx_trigger_response(message)
        return self._render_config(
            request,
            workflow,
            validator,
            config_form,
            step,
            status=200,
        )

    # Helper methods ---------------------------------------------------------

    def _get_step(self) -> WorkflowStep | None:
        step_id = self.kwargs.get("step_id")
        if not step_id:
            return None
        workflow = self.get_workflow()
        return get_object_or_404(WorkflowStep, workflow=workflow, pk=step_id)

    def _available_validators(self, workflow: Workflow) -> list[Validator]:
        return list(Validator.objects.all().order_by("validation_type", "name", "pk"))

    def _build_config_form(
        self,
        validator: Validator,
        *,
        step: WorkflowStep | None,
        data=None,
        files=None,
    ) -> forms.Form:
        form_class = get_config_form_class(validator.validation_type)
        kwargs = {"step": step}
        if data is not None or files is not None:
            return form_class(data or None, files or None, **kwargs)
        return form_class(**kwargs)

    def _render_select(self, request, workflow: Workflow, form=None, status=200):
        validators = self._available_validators(workflow)
        selected_id = None
        if form is not None:
            selected_id = form.data.get("validator") or form.initial.get("validator")
        else:
            selected_id = request.GET.get("selected")

        context = {
            "workflow": workflow,
            "form": form or WorkflowStepTypeForm(validators=validators),
            "validators": validators,
            "max_step_count": MAX_STEP_COUNT,
            "step": None,
            "limit_reached": False,
            "selected_validator": str(selected_id) if selected_id else None,
        }
        return render(request, self.template_select, context, status=status)

    def _render_config(
        self,
        request,
        workflow: Workflow,
        validator: Validator,
        form: forms.Form,
        step: WorkflowStep | None,
        status: int = 200,
    ):
        context = {
            "workflow": workflow,
            "validator": validator,
            "form": form,
            "step": step,
            "max_step_count": MAX_STEP_COUNT,
        }
        return render(request, self.template_config, context, status=status)

    def _resolve_validator(
        self,
        request,
        step: WorkflowStep | None,
    ) -> Validator | None:
        if step is not None:
            return step.validator
        validator_id = request.POST.get("validator") or request.POST.get("validator_id")
        if not validator_id:
            return None
        return get_object_or_404(Validator, pk=validator_id)

    def _save_step(
        self,
        workflow: Workflow,
        validator: Validator,
        form: forms.Form,
        *,
        step: WorkflowStep | None,
    ) -> WorkflowStep:
        is_new = step is None
        step = step or WorkflowStep(workflow=workflow)
        step.validator = validator
        step.name = form.cleaned_data.get("name", "").strip() or validator.name

        config: dict[str, Any]
        ruleset: Ruleset | None = None
        vtype = validator.validation_type

        if vtype == ValidationType.JSON_SCHEMA:
            config, ruleset = self._build_json_schema(workflow, form, step)
        elif vtype == ValidationType.XML_SCHEMA:
            config, ruleset = self._build_xml_schema(workflow, form, step)
        elif vtype == ValidationType.ENERGYPLUS:
            config = self._build_energyplus_config(form)
        elif vtype == ValidationType.AI_ASSIST:
            config = self._build_ai_config(form)
        else:
            config = {}

        if ruleset is not None:
            step.ruleset = ruleset
        elif vtype not in (ValidationType.JSON_SCHEMA, ValidationType.XML_SCHEMA):
            step.ruleset = None

        step.config = config

        if is_new:
            max_order = workflow.steps.aggregate(max_order=models.Max("order"))[
                "max_order"
            ]
            step.order = (max_order or 0) + 10

        step.save()
        return step

    def _ensure_ruleset(
        self,
        *,
        workflow: Workflow,
        step: WorkflowStep | None,
        ruleset_type: str,
    ) -> Ruleset:
        if step and step.ruleset and step.ruleset.ruleset_type == ruleset_type:
            ruleset = step.ruleset
        else:
            ruleset = Ruleset(org=workflow.org, user=workflow.user)
        ruleset.org = workflow.org
        ruleset.user = workflow.user
        ruleset.ruleset_type = ruleset_type
        ruleset.name = ruleset.name or f"ruleset-{uuid4().hex[:8]}"
        ruleset.version = ruleset.version or "1"
        return ruleset

    def _build_json_schema(
        self,
        workflow: Workflow,
        form: JsonSchemaStepConfigForm,
        step: WorkflowStep | None,
    ) -> tuple[dict[str, Any], Ruleset | None]:
        source = form.cleaned_data.get("schema_source")
        text = (form.cleaned_data.get("schema_text") or "").strip()
        uploaded = form.cleaned_data.get("schema_file")

        if source == "keep" and step and step.ruleset_id:
            preview = step.config.get("schema_text_preview", "")
            config = {"schema_source": "keep", "schema_text_preview": preview}
            return config, step.ruleset

        ruleset = self._ensure_ruleset(
            workflow=workflow,
            step=step,
            ruleset_type=RulesetType.JSON_SCHEMA,
        )

        if source == "text":
            content = ContentFile(
                text.encode("utf-8"),
                name=f"schema-{uuid4().hex}.json",
            )
            if ruleset.file:
                ruleset.file.delete(save=False)
            ruleset.file.save(content.name, content, save=False)
            preview = text[:1200]
        else:
            if ruleset.file:
                ruleset.file.delete(save=False)
            ruleset.file.save(uploaded.name, uploaded, save=False)
            preview = ""

        ruleset.metadata = {"kind": "json"}
        ruleset.full_clean()
        ruleset.save()

        config = {"schema_source": source, "schema_text_preview": preview}
        return config, ruleset

    def _build_xml_schema(
        self,
        workflow: Workflow,
        form: XmlSchemaStepConfigForm,
        step: WorkflowStep | None,
    ) -> tuple[dict[str, Any], Ruleset | None]:
        source = form.cleaned_data.get("schema_source")
        text = (form.cleaned_data.get("schema_text") or "").strip()
        uploaded = form.cleaned_data.get("schema_file")
        schema_type = form.cleaned_data.get("schema_type")

        if source == "keep" and step and step.ruleset_id:
            preview = step.config.get("schema_text_preview", "")
            config = {
                "schema_source": "keep",
                "schema_type": schema_type or step.config.get("schema_type"),
                "schema_text_preview": preview,
            }
            return config, step.ruleset

        ruleset = self._ensure_ruleset(
            workflow=workflow,
            step=step,
            ruleset_type=RulesetType.XML_SCHEMA,
        )

        extension = {
            XMLSchemaType.DTD: ".dtd",
            XMLSchemaType.XSD: ".xsd",
            XMLSchemaType.RELAXNG: ".rng",
        }.get(schema_type, ".xml")

        if source == "text":
            content = ContentFile(
                text.encode("utf-8"),
                name=f"schema-{uuid4().hex}{extension}",
            )
            if ruleset.file:
                ruleset.file.delete(save=False)
            ruleset.file.save(content.name, content, save=False)
            preview = text[:1200]
        else:
            if ruleset.file:
                ruleset.file.delete(save=False)
            ruleset.file.save(uploaded.name, uploaded, save=False)
            preview = ""

        ruleset.metadata = {"schema_type": schema_type}
        ruleset.full_clean()
        ruleset.save()

        config = {
            "schema_source": source,
            "schema_type": schema_type,
            "schema_text_preview": preview,
            "schema_type_label": XMLSchemaType(schema_type).label
            if schema_type in XMLSchemaType.values
            else schema_type,
        }
        return config, ruleset

    def _build_energyplus_config(
        self,
        form: EnergyPlusStepConfigForm,
    ) -> dict[str, Any]:
        eui_min = form.cleaned_data.get("eui_min")
        eui_max = form.cleaned_data.get("eui_max")
        eui_band = {
            "min": float(eui_min) if eui_min is not None else None,
            "max": float(eui_max) if eui_max is not None else None,
        }
        return {
            "run_simulation": form.cleaned_data.get("run_simulation", False),
            "idf_checks": form.cleaned_data.get("idf_checks", []),
            "simulation_checks": form.cleaned_data.get("simulation_checks", []),
            "eui_band": eui_band,
            "notes": form.cleaned_data.get("notes", ""),
        }

    def _build_ai_config(self, form: AiAssistStepConfigForm) -> dict[str, Any]:
        selectors = form.cleaned_data.get("selectors", [])
        policy_rules = form.cleaned_data.get("policy_rules", [])
        return {
            "template": form.cleaned_data.get("template"),
            "mode": form.cleaned_data.get("mode"),
            "cost_cap_cents": form.cleaned_data.get("cost_cap_cents"),
            "selectors": selectors,
            "policy_rules": [asdict(rule) for rule in policy_rules],
        }


class WorkflowStepDeleteView(WorkflowObjectMixin, View):
    def post(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        step = get_object_or_404(
            WorkflowStep,
            workflow=workflow,
            pk=self.kwargs.get("step_id"),
        )
        step.delete()
        _resequence_workflow_steps(workflow)
        message = _("Workflow step removed.")
        return _hx_trigger_response(message, close_modal=None)


class WorkflowStepMoveView(WorkflowObjectMixin, View):
    def post(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        step = get_object_or_404(
            WorkflowStep,
            workflow=workflow,
            pk=self.kwargs.get("step_id"),
        )
        direction = request.POST.get("direction")
        steps = list(workflow.steps.all().order_by("order", "pk"))
        try:
            index = steps.index(step)
        except ValueError:
            return _hx_trigger_response(
                status_code=400,
                message=_("Step not found."),
                level="warning",
            )
        if direction == "up" and index > 0:
            steps[index - 1], steps[index] = steps[index], steps[index - 1]
        elif direction == "down" and index < len(steps) - 1:
            steps[index], steps[index + 1] = steps[index + 1], steps[index]
        else:
            return _hx_trigger_response(status_code=204)
        with transaction.atomic():
            for pos, item in enumerate(steps, start=1):
                WorkflowStep.objects.filter(pk=item.pk).update(order=1000 + pos)
            _resequence_workflow_steps(workflow)
        message = _("Workflow step order updated.")
        return _hx_trigger_response(message, close_modal=None)


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
            {
                "name": _("Workflows"),
                "url": reverse_with_org(
                    "workflows:workflow_list",
                    request=self.request,
                ),
            },
        )
        breadcrumbs.append(
            {
                "name": workflow.name,
                "url": reverse_with_org(
                    "workflows:workflow_detail",
                    request=self.request,
                    kwargs={"pk": workflow.pk},
                ),
            },
        )
        breadcrumbs.append({"name": _("Validations"), "url": ""})
        return breadcrumbs
