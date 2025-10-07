import base64
import json
import logging
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.files.base import ContentFile
from django.db import models, transaction
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import reverse, reverse_lazy
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import DetailView, ListView, UpdateView
from django.views.generic.edit import CreateView, DeleteView
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from simplevalidations.core.mixins import BreadcrumbMixin
from simplevalidations.projects.models import Project
from simplevalidations.submissions.ingest import (
    prepare_inline_text,
    prepare_uploaded_file,
)
from simplevalidations.submissions.models import Submission
from simplevalidations.tracking.services import TrackingEventService
from simplevalidations.users.models import User
from simplevalidations.validations.constants import (
    RulesetType,
    ValidationType,
    XMLSchemaType,
)
from simplevalidations.validations.models import Ruleset, ValidationRun, Validator
from simplevalidations.validations.serializers import ValidationRunStartSerializer
from simplevalidations.validations.services.validation_run import ValidationRunService
from simplevalidations.workflows.constants import SUPPORTED_CONTENT_TYPES
from simplevalidations.workflows.forms import (
    AiAssistStepConfigForm,
    EnergyPlusStepConfigForm,
    JsonSchemaStepConfigForm,
    WorkflowForm,
    WorkflowStepTypeForm,
    XmlSchemaStepConfigForm,
    get_config_form_class,
)
from simplevalidations.workflows.models import Workflow, WorkflowStep
from simplevalidations.workflows.request_utils import (
    extract_request_basics,
    is_raw_body_mode,
)
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

        # TODO: When we support projects, we need to resolve the project here.
        # project = self._resolve_project(workflow=workflow, request=request)
        project = None  # We don't support projects yet

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
        except Exception as e:  # noqa: BLE001
            logger.info(
                "ValidationRunStartSerializer invalid: %s",
                getattr(e, "detail", str(e)),
            )
            raise e

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
            .prefetch_related("validation_runs")
            .order_by("name", "-version")
        )

    def get_queryset(self):
        return self.get_workflow_queryset()


class WorkflowObjectMixin(WorkflowAccessMixin):
    workflow_url_kwarg = "pk"

    def get_workflow(self) -> Workflow:
        if not hasattr(self, "_workflow"):
            queryset = (
                self.get_workflow_queryset()
                .select_related("org", "user")
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

    def get_queryset(self):
        return super().get_queryset().prefetch_related("steps")

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
                "max_step_count": MAX_STEP_COUNT,
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
                    request, workflow, validator, config_form, None
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
                message = _(
                    "You can add up to %(count)s steps per workflow."
                    % {"count": MAX_STEP_COUNT}
                )
                return _hx_trigger_response(message, level="warning", status_code=409)
            self._save_step(workflow, validator, config_form, step=step)
            _resequence_workflow_steps(workflow)
            message = _("Workflow step saved.")
            return _hx_trigger_response(message)
        return self._render_config(
            request, workflow, validator, config_form, step, status=200
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
        self, request, step: WorkflowStep | None
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
                text.encode("utf-8"), name=f"schema-{uuid4().hex}.json"
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
                text.encode("utf-8"), name=f"schema-{uuid4().hex}{extension}"
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
        self, form: EnergyPlusStepConfigForm
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
                status_code=400, message=_("Step not found."), level="warning"
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
