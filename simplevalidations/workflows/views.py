import base64
import contextlib
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
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError
from django.db import models
from django.db import transaction
from django.db.models import Q
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
from django.views.generic.edit import FormView
from rest_framework import permissions
from rest_framework import status
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from simplevalidations.actions.constants import ActionCategoryType
from simplevalidations.actions.models import ActionDefinition
from simplevalidations.actions.models import SignedCertificateAction
from simplevalidations.actions.models import SlackMessageAction
from simplevalidations.actions.registry import get_action_form
from simplevalidations.core.mixins import BreadcrumbMixin
from simplevalidations.core.utils import pretty_json
from simplevalidations.core.utils import pretty_xml
from simplevalidations.core.utils import reverse_with_org
from simplevalidations.projects.models import Project
from simplevalidations.submissions.ingest import prepare_inline_text
from simplevalidations.submissions.ingest import prepare_uploaded_file
from simplevalidations.submissions.models import Submission
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.models import Organization
from simplevalidations.users.models import User
from simplevalidations.validations.constants import AssertionType
from simplevalidations.validations.constants import JSONSchemaVersion
from simplevalidations.validations.constants import RulesetType
from simplevalidations.validations.constants import ValidationRunStatus
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.constants import XMLSchemaType
from simplevalidations.validations.forms import RulesetAssertionForm
from simplevalidations.validations.models import Ruleset
from simplevalidations.validations.models import RulesetAssertion
from simplevalidations.validations.models import ValidationRun
from simplevalidations.validations.models import Validator
from simplevalidations.validations.serializers import ValidationRunStartSerializer
from simplevalidations.validations.services.validation_run import ValidationRunService
from simplevalidations.workflows.constants import SUPPORTED_CONTENT_TYPES
from simplevalidations.workflows.constants import WorkflowStartErrorCode
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
ADVANCED_VALIDATION_TYPES = {
    ValidationType.BASIC,
    ValidationType.ENERGYPLUS,
    ValidationType.CUSTOM_RULES,
}


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

        executor_qs = Workflow.objects.for_user(
            user,
            required_role_code=RoleCode.EXECUTOR,
        )
        has_executor_role = executor_qs.filter(pk=workflow.pk).exists()
        if not has_executor_role:
            # Return 404 to avoid leaking workflow existence when user lacks access.
            raise Http404

        if not workflow.is_active:
            return Response(
                {
                    "detail": "",
                    "code": WorkflowStartErrorCode.WORKFLOW_INACTIVE,
                },
                status=status.HTTP_409_CONFLICT,
            )

        if not workflow.steps.exists():
            return Response(
                {
                    "detail": _(
                        "This workflow has no steps defined and cannot be executed.",
                    ),
                    "code": WorkflowStartErrorCode.NO_WORKFLOW_STEPS,
                },
                status=status.HTTP_400_BAD_REQUEST,
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
            logger.info("Started ValidationRun %s with status %s", run_id, run_status)

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
    """
    Reusable helpers for workflow UI views.
    """

    manager_role_codes = {
        RoleCode.OWNER,
        RoleCode.ADMIN,
        RoleCode.AUTHOR,
    }

    def get_workflow_queryset(self):
        user = self.request.user
        queryset = (
            Workflow.objects.for_user(user)
            .select_related("org", "user", "project")
            .prefetch_related("validation_runs")
            .order_by("name", "-version")
        )
        current_org = None
        if hasattr(user, "get_current_org"):
            current_org = user.get_current_org()
        if current_org:
            return queryset.filter(org=current_org)
        return queryset.none()

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
    extra_payload: dict[str, object] | None = None,
) -> HttpResponse:
    response = HttpResponse(status=status_code)
    payload: dict[str, object] = {"steps-changed": True}
    if extra_payload:
        payload.update(extra_payload)
    if message:
        payload["toast"] = {"level": level, "message": str(message)}
    if close_modal:
        payload["close-modal"] = close_modal
    response["HX-Trigger"] = json.dumps(payload)
    return response


def _hx_redirect_response(url: str) -> HttpResponse:
    response = HttpResponse(status=204)
    response["HX-Redirect"] = url
    return response


def _ensure_ruleset(
    *,
    workflow: Workflow,
    step: WorkflowStep | None,
    ruleset_type: str,
) -> Ruleset:
    """
    Ensure a ruleset exists for the given workflow step and type.
    """

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


def _ensure_advanced_ruleset(
    workflow: Workflow,
    step: WorkflowStep | None,
    validator: Validator,
) -> Ruleset:
    """Guarantee a ruleset exists for validators requiring assertions."""
    ruleset = getattr(step, "ruleset", None)
    if ruleset is None:
        base_name = f"{validator.slug}-ruleset"
        ruleset_name = _unique_ruleset_name(
            org=workflow.org,
            ruleset_type=validator.validation_type,
            base_name=base_name,
            version="1",
        )
        ruleset = Ruleset(
            org=workflow.org,
            user=workflow.user,
            name=ruleset_name,
            ruleset_type=validator.validation_type,
            version="1",
        )
        ruleset.save()
        if step:
            step.ruleset = ruleset
            if step.pk:
                step.save(update_fields=["ruleset", "modified"])
    return ruleset


def _unique_ruleset_name(
    *,
    org: Organization,
    ruleset_type: str,
    base_name: str,
    version: str,
) -> str:
    name = base_name
    suffix = 2
    while Ruleset.objects.filter(
        org=org,
        ruleset_type=ruleset_type,
        name=name,
        version=version,
    ).exists():
        truncated_base = base_name[: max(0, 240)]
        name = f"{truncated_base}-{suffix}"
        suffix += 1
    return name


def _build_json_schema_config(
    workflow: Workflow,
    form: JsonSchemaStepConfigForm,
    step: WorkflowStep | None,
) -> tuple[dict[str, Any], Ruleset | None]:
    source = form.cleaned_data.get("schema_source")
    text = (form.cleaned_data.get("schema_text") or "").strip()
    uploaded = form.cleaned_data.get("schema_file")
    schema_type = form.cleaned_data.get("schema_type")

    if schema_type not in JSONSchemaVersion.values:
        raise ValidationError(_("Select a valid JSON Schema draft."))

    if source == "keep" and step and step.ruleset_id:
        preview = step.config.get("schema_text_preview", "")
        ruleset = step.ruleset
        metadata = dict(ruleset.metadata or {})
        metadata["schema_type"] = schema_type
        metadata.pop("schema", None)
        ruleset.metadata = metadata
        ruleset.full_clean()
        ruleset.save(update_fields=["metadata"])
        config = {
            "schema_source": "keep",
            "schema_text_preview": preview,
            "schema_type": schema_type,
            "schema_type_label": str(JSONSchemaVersion(schema_type).label),
        }
        return config, ruleset

    ruleset = _ensure_ruleset(
        workflow=workflow,
        step=step,
        ruleset_type=RulesetType.JSON_SCHEMA,
    )

    schema_payload: str | None = None

    if source == "text":
        ruleset.rules_text = text
        if ruleset.rules_file:
            ruleset.rules_file.delete(save=False)
        ruleset.rules_file = None
        schema_payload = text
        preview = text[:1200]
    else:
        if uploaded is None:
            raise ValidationError(_("Upload a JSON schema file."))
        uploaded.seek(0)
        raw_bytes = uploaded.read()
        uploaded.seek(0)
        if ruleset.rules_file:
            ruleset.rules_file.delete(save=False)
        ruleset.rules_file.save(uploaded.name, uploaded, save=False)
        ruleset.rules_text = ""
        schema_payload = (
            raw_bytes.decode("utf-8", errors="replace")
            if isinstance(raw_bytes, bytes)
            else str(raw_bytes or "")
        )
        preview = schema_payload[:1200]

    metadata = dict(ruleset.metadata or {})
    metadata["schema_type"] = schema_type
    metadata.pop("schema", None)
    ruleset.metadata = metadata
    ruleset.full_clean()
    ruleset.save()

    config = {
        "schema_source": source,
        "schema_text_preview": preview,
        "schema_type": schema_type,
        "schema_type_label": str(JSONSchemaVersion(schema_type).label),
    }
    return config, ruleset


def _build_xml_schema_config(
    workflow: Workflow,
    form: XmlSchemaStepConfigForm,
    step: WorkflowStep | None,
) -> tuple[dict[str, Any], Ruleset | None]:
    source = form.cleaned_data.get("schema_source")
    text = (form.cleaned_data.get("schema_text") or "").strip()
    uploaded = form.cleaned_data.get("schema_file")
    schema_type = form.cleaned_data.get("schema_type")

    if schema_type not in XMLSchemaType.values:
        raise ValidationError(_("Select a valid XML schema type."))

    if source == "keep" and step and step.ruleset_id:
        preview = step.config.get("schema_text_preview", "")
        ruleset = step.ruleset
        metadata = dict(ruleset.metadata or {})
        metadata["schema_type"] = schema_type
        metadata.pop("schema", None)
        ruleset.metadata = metadata
        ruleset.full_clean()
        ruleset.save(update_fields=["metadata"])
        config = {
            "schema_source": "keep",
            "schema_type": schema_type,
            "schema_text_preview": preview,
            "schema_type_label": str(XMLSchemaType(schema_type).label),
        }
        return config, ruleset

    ruleset = _ensure_ruleset(
        workflow=workflow,
        step=step,
        ruleset_type=RulesetType.XML_SCHEMA,
    )

    if source == "text":
        ruleset.rules_text = text
        if ruleset.rules_file:
            ruleset.rules_file.delete(save=False)
        ruleset.rules_file = None
        preview = text[:1200]
    else:
        if uploaded is None:
            raise ValidationError(_("Upload an XML schema file."))
        uploaded.seek(0)
        raw_bytes = uploaded.read()
        uploaded.seek(0)
        if ruleset.rules_file:
            ruleset.rules_file.delete(save=False)
        ruleset.rules_file.save(uploaded.name, uploaded, save=False)
        ruleset.rules_text = ""
        schema_payload = (
            raw_bytes.decode("utf-8", errors="replace")
            if isinstance(raw_bytes, bytes)
            else str(raw_bytes or "")
        )
        preview = schema_payload[:1200]

    metadata = dict(ruleset.metadata or {})
    metadata["schema_type"] = schema_type
    metadata.pop("schema", None)
    ruleset.metadata = metadata
    ruleset.full_clean()
    ruleset.save()

    config = {
        "schema_source": source,
        "schema_type": schema_type,
        "schema_text_preview": preview,
        "schema_type_label": str(XMLSchemaType(schema_type).label),
    }
    return config, ruleset


def _build_energyplus_config(form: EnergyPlusStepConfigForm) -> dict[str, Any]:
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
        "notes": form.cleaned_data.get("energyplus_notes", ""),
    }


def _build_ai_config(form: AiAssistStepConfigForm) -> dict[str, Any]:
    selectors = form.cleaned_data.get("selectors", [])
    policy_rules = form.cleaned_data.get("policy_rules", [])
    return {
        "template": form.cleaned_data.get("template"),
        "mode": form.cleaned_data.get("mode"),
        "cost_cap_cents": form.cleaned_data.get("cost_cap_cents"),
        "selectors": selectors,
        "policy_rules": [asdict(rule) for rule in policy_rules],
    }


def save_workflow_step(
    workflow: Workflow,
    validator: Validator,
    form: forms.Form,
    *,
    step: WorkflowStep | None = None,
) -> WorkflowStep:
    """
    Persist a workflow step using the supplied form data and validator.
    """
    is_new = step is None
    step = step or WorkflowStep(workflow=workflow)
    step.validator = validator
    step.action = None
    step.name = form.cleaned_data.get("name", "").strip() or validator.name
    step.description = (form.cleaned_data.get("description") or "").strip()
    step.notes = (form.cleaned_data.get("notes") or "").strip()
    if "display_schema" in form.cleaned_data:
        step.display_schema = form.cleaned_data.get("display_schema", False)

    config: dict[str, Any]
    ruleset: Ruleset | None = None
    vtype = validator.validation_type

    if vtype == ValidationType.JSON_SCHEMA:
        config, ruleset = _build_json_schema_config(workflow, form, step)
    elif vtype == ValidationType.XML_SCHEMA:
        config, ruleset = _build_xml_schema_config(workflow, form, step)
    elif vtype == ValidationType.ENERGYPLUS:
        config = _build_energyplus_config(form)
    elif vtype == ValidationType.AI_ASSIST:
        config = _build_ai_config(form)
    else:
        config = {}

    if ruleset is not None:
        step.ruleset = ruleset
    elif vtype in ADVANCED_VALIDATION_TYPES:
        step.ruleset = _ensure_advanced_ruleset(workflow, step, validator)
    elif vtype not in (ValidationType.JSON_SCHEMA, ValidationType.XML_SCHEMA):
        step.ruleset = None

    step.config = config

    if is_new:
        max_order = workflow.steps.aggregate(max_order=models.Max("order"))["max_order"]
        step.order = (max_order or 0) + 10

    step.save()
    return step


def save_workflow_action_step(
    workflow: Workflow,
    definition: ActionDefinition,
    form: forms.Form,
    *,
    step: WorkflowStep | None = None,
) -> WorkflowStep:
    """Persist a workflow step that references an action definition."""

    is_new = step is None
    step = step or WorkflowStep(workflow=workflow)
    action = getattr(step, "action", None)

    if not hasattr(form, "save_action"):
        raise ValueError("Action forms must implement save_action().")

    action = form.save_action(
        definition,
        current_action=action,
    )

    step.validator = None
    step.ruleset = None
    step.action = action
    step.name = action.name
    step.description = action.description
    step.notes = (form.cleaned_data.get("notes") or "").strip()
    step.display_schema = False
    summary = {}
    if hasattr(form, "build_step_summary"):
        summary = form.build_step_summary(action) or {}
    step.config = summary

    if is_new:
        max_order = workflow.steps.aggregate(max_order=models.Max("order"))["max_order"]
        step.order = (max_order or 0) + 10

    step.save()
    return step


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
        return (
            super()
            .get_queryset()
            .prefetch_related(
                "steps__validator",
                "steps__ruleset",
                "steps__action",
                "steps__action__definition",
            )
        )

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
                "show_private_notes": self.user_can_manage_workflow(),
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
    """
    This mixin provides helper methods to build context for launching workflows
    via the UI. It also provides methods to get recent runs and load a specific run
    for display.

    Args:
        WorkflowObjectMixin (_type_): _description_

    Returns:
        _type_: _description_
    """

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
        has_steps = workflow.steps.exists()
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
            "has_steps": has_steps,
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
        has_steps = workflow.steps.exists()
        form = (
            self.get_launch_form(workflow=workflow)
            if can_execute and has_steps
            else None
        )
        context.update(
            {
                "workflow": workflow,
                "recent_runs": self.get_recent_runs(workflow),
                "can_execute": can_execute,
                "has_steps": has_steps,
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
    """
    Handle POST to start a workflow run from an HTML-based form.
    This view is meant to be used in conjunction with WorkflowLaunchDetailView.

    For API calls to start a workflow run, use the WorkflowViewSet.start_validation
    action.

    """

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

        if not workflow.steps.exists():
            return self._launch_response(
                request,
                workflow=workflow,
                form=None,
                active_run=None,
                status_code=400,
                toast={
                    "level": "warning",
                    "message": str(
                        _("This workflow has no steps defined and cannot be executed."),
                    ),
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
                "Failed to prepare submission for workflow run.",
                exc_info=exc,
            )
            form.add_error(
                None,
                _("Something went wrong while preparing the submission."),
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
            form.add_error(None, _("You do not have permission to run this workflow."))
            return self._launch_response(
                request,
                workflow=workflow,
                form=form,
                active_run=None,
                status_code=403,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "Run service errored for workflow %s",
                workflow.pk,
                exc_info=exc,
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
            max_file = int(settings.SUBMISSION_FILE_MAX_BYTES)
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


class PublicWorkflowListView(ListView):
    """
    Public listing of workflows available to visitors and signed-in members.

    Example:
        /workflows/?q=data&layout=list&per_page=100
    """

    template_name = "workflows/public/workflow_list.html"
    context_object_name = "workflows"
    paginate_by = 50
    page_size_options = (10, 50, 100)
    http_method_names = ["get"]

    def get_queryset(self):
        user = self.request.user
        queryset = Workflow.objects.filter(is_active=True)
        if user.is_authenticated:
            accessible_ids = (
                Workflow.objects.for_user(user)
                .filter(is_active=True)
                .values_list("pk", flat=True)
            )
            queryset = queryset.filter(
                models.Q(make_info_public=True) | models.Q(pk__in=accessible_ids),
            )
        else:
            queryset = queryset.filter(make_info_public=True)

        search_query = self.request.GET.get("q", "").strip()
        if search_query:
            queryset = queryset.filter(
                models.Q(name__icontains=search_query)
                | models.Q(slug__icontains=search_query),
            )

        return (
            queryset.select_related("org", "project", "user")
            .prefetch_related("steps")
            .order_by("name", "pk")
            .distinct()
        )

    def get_paginate_by(self, queryset):
        per_page = self.request.GET.get("per_page")
        page_size = self.paginate_by
        if per_page:
            try:
                per_page_value = int(per_page)
            except (TypeError, ValueError):
                per_page_value = self.paginate_by
            else:
                if per_page_value in self.page_size_options:
                    page_size = per_page_value
        self.page_size = page_size
        return page_size

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        layout = self._get_layout()
        query_string = self._build_query_params()
        context.update(
            {
                "search_query": self.request.GET.get("q", ""),
                "current_layout": layout,
                "layout_urls": {
                    "grid": self._build_url_with_params(layout="grid"),
                    "list": self._build_url_with_params(layout="list"),
                },
                "query_string": query_string,
                "page_size_options": self.page_size_options,
                "current_page_size": getattr(self, "page_size", self.paginate_by),
                "page_title": _("All Workflows"),
                "breadcrumbs": [
                    {"name": _("All Workflows"), "url": ""},
                ],
            },
        )
        return context

    def _get_layout(self) -> str:
        layout = self.request.GET.get("layout", "grid")
        if layout not in {"grid", "list"}:
            return "grid"
        return layout

    def _build_query_params(self, **overrides) -> str:
        params = self.request.GET.copy()
        params.pop("page", None)
        for key, value in overrides.items():
            if value is None:
                params.pop(key, None)
            else:
                params[key] = value
        return params.urlencode()

    def _build_url_with_params(self, **overrides) -> str:
        query = self._build_query_params(**overrides)
        return f"?{query}" if query else "?"


class WorkflowPublicInfoView(DetailView):
    """
    Handles public display of workflow information for visitors.
    This is a read-only view showing workflow details and recent runs,
    available to the public if the workflow is marked as public, and
    to authenticated users who have access to the workflow.

    If an authenticated user has access to the workflow and wants to launch
    the workflow, a control is provided to navigate to the launch page.
    """

    template_name = "workflows/public/workflow_info.html"
    context_object_name = "workflow"
    slug_field = "uuid"
    slug_url_kwarg = "workflow_uuid"

    def get_queryset(self):
        return (
            Workflow.objects.filter(make_info_public=True)
            .select_related("org", "project", "user")
            .prefetch_related(
                "steps",
                "steps__validator",
                "steps__ruleset",
                "steps__action",
                "steps__action__definition",
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = context["workflow"]
        user = self.request.user
        steps = list(workflow.steps.all().order_by("order"))
        self._annotate_public_schema_steps(steps)
        context.update(
            {
                "steps": steps,
                "recent_runs": list(
                    workflow.validation_runs.select_related("user").order_by(
                        "-created",
                    )[:5],
                ),
                "user_has_access": (
                    user.is_authenticated and workflow.can_execute(user=user)
                ),
                "breadcrumbs": [
                    {
                        "name": _("All Workflows"),
                        "url": reverse("public_workflow_list"),
                    },
                    {
                        "name": _("Workflow '%(name)s'") % {"name": workflow.name},
                        "url": "",
                    },
                ],
            },
        )
        return context

    def _annotate_public_schema_steps(self, steps: list[WorkflowStep]) -> None:
        for step in steps:
            step.public_schema = None
            step.public_action_meta = None
            step.public_action_summary = {}

            if step.validator is None:
                if step.action:
                    self._populate_public_action(step)
                continue

            vtype = step.validator.validation_type
            if vtype not in {ValidationType.JSON_SCHEMA, ValidationType.XML_SCHEMA}:
                continue

            schema_content: str | None = None
            schema_language: str | None = None
            if step.display_schema:
                schema_content, schema_language = self._load_schema_content(step)

            if schema_content:
                step.public_schema = {
                    "content": schema_content,
                    "language": schema_language
                    or ("json" if vtype == ValidationType.JSON_SCHEMA else "xml"),
                }

    def _populate_public_action(self, step: WorkflowStep) -> None:
        action = step.action
        definition = action.definition
        variant = action.get_variant()
        summary: dict[str, str] = {}

        if isinstance(variant, SlackMessageAction):
            summary["message"] = variant.message
        elif isinstance(variant, SignedCertificateAction):
            summary["certificate_template"] = (
                variant.get_certificate_template_display_name()
            )

        step.public_action_meta = {
            "category_label": definition.get_action_category_display(),
            "type": definition.type,
            "icon": definition.icon or "bi-gear",
            "definition_name": definition.name,
        }
        step.public_action_summary = summary

    def _load_schema_content(
        self,
        step: WorkflowStep,
    ) -> tuple[str | None, str | None]:
        """
        Load and pretty-print schema content for public display.


        Args:
            step (WorkflowStep)

        Returns:
            tuple[str | None, str | None] : (pretty_schema_content, language)
        """
        schema_text: str = ""
        if step.ruleset:
            try:
                schema_text = step.ruleset.rules
            except Exception:
                logger.exception(
                    "Failed to load rules for step",
                    extra={"step_id": step.pk},
                )
                schema_text = ""

        if not schema_text:
            schema_text = step.config.get("schema_text_preview", "")

        if not schema_text:
            return None, None

        vtype = step.validator.validation_type
        if vtype == ValidationType.JSON_SCHEMA:
            try:
                pretty = pretty_json(schema_text)
            except Exception:
                pretty = schema_text
            return pretty, "json"

        if vtype == ValidationType.XML_SCHEMA:
            try:
                pretty = pretty_xml(schema_text)
            except Exception:
                pretty = schema_text
            return pretty, "xml"

        return schema_text, None


class WorkflowFormViewMixin(WorkflowAccessMixin):
    form_class = WorkflowForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        form = context.get("form")
        context["project_context"] = self._project_from_form(form) if form else None
        return context

    def _project_from_form(self, form: WorkflowForm) -> Project | None:
        if not form:
            return None
        project = getattr(form.instance, "project", None)
        if project:
            return project
        project_id = form.initial.get("project") or form.data.get("project")
        if not project_id:
            default = self._default_project_for_org()
            if default:
                if isinstance(form.initial, dict):
                    form.initial.setdefault("project", default.pk)
                return default
            return None
        try:
            return Project.objects.get(pk=project_id)
        except (Project.DoesNotExist, ValueError, TypeError):
            return None

    def _default_project_for_org(self) -> Project | None:
        user = getattr(self.request, "user", None)
        org = getattr(user, "get_current_org", lambda: None)() if user else None
        if not org:
            return None
        project = Project.objects.filter(org=org, is_default=True).first()
        if project:
            return project
        return Project.objects.filter(org=org).order_by("name").first()


class WorkflowCreateView(WorkflowFormViewMixin, CreateView):
    template_name = "workflows/workflow_form.html"

    def get_initial(self):
        initial = super().get_initial()
        project = self._project_from_request() or self._default_project_for_org()
        if project:
            initial["project"] = project.pk
        return initial

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

    def _project_from_request(self) -> Project | None:
        project_id = self.request.GET.get("project")
        if not project_id:
            return None
        user = self.request.user
        org = getattr(user, "get_current_org", lambda: None)()
        if not org:
            return None
        try:
            return Project.objects.get(pk=project_id, org=org)
        except (Project.DoesNotExist, ValueError, TypeError):
            return None


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
                        "Workflow reactivated. New validation "
                        "runs can start immediately.",
                    ),
                )
            else:
                messages.info(
                    request,
                    _(
                        "Workflow disabled. Existing runs finish, "
                        "but new ones are blocked.",
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
        steps = (
            workflow.steps.all()
            .order_by("order", "pk")
            .select_related("validator", "ruleset", "action", "action__definition")
        )
        for step in steps:
            config = dict(step.config or {})
            if step.validator:
                vtype = step.validator.validation_type
                if vtype == ValidationType.ENERGYPLUS:
                    band = config.get("eui_band") or {}
                    config.setdefault(
                        "eui_band",
                        {
                            "min": band.get("min"),
                            "max": band.get("max"),
                        },
                    )
                elif vtype == ValidationType.XML_SCHEMA:
                    schema_type = config.get("schema_type")
                    if schema_type:
                        try:
                            config["schema_type_label"] = XMLSchemaType(
                                schema_type,
                            ).label
                        except ValueError:
                            config["schema_type_label"] = schema_type
                elif vtype == ValidationType.JSON_SCHEMA:
                    schema_type = config.get("schema_type")
                    if schema_type:
                        try:
                            config["schema_type_label"] = JSONSchemaVersion(
                                schema_type
                            ).label
                        except ValueError:
                            config["schema_type_label"] = schema_type
            elif step.action:
                definition = step.action.definition
                variant = step.action.get_variant()
                step.action_variant = variant
                if not config and variant:
                    if isinstance(variant, SlackMessageAction):
                        config["message"] = variant.message
                    elif isinstance(variant, SignedCertificateAction):
                        config["certificate_template"] = (
                            variant.get_certificate_template_display_name()
                        )
                step.action_meta = {
                    "category_label": definition.get_action_category_display(),
                    "type": definition.type,
                    "icon": definition.icon or "bi-gear",
                    "definition_name": definition.name,
                    "definition_description": definition.description,
                }
                extras = {
                    key: value
                    for key, value in config.items()
                    if key not in {"message", "certificate_template"}
                }
                step.action_summary = {
                    "message": config.get("message"),
                    "certificate_template": config.get("certificate_template"),
                    "extras": extras,
                }
            step.config = config
        show_private_notes = self.user_can_manage_workflow()
        context = {
            "workflow": workflow,
            "steps": steps,
            "max_step_count": MAX_STEP_COUNT,
            "show_private_notes": show_private_notes,
        }
        return render(request, self.template_name, context)


class WorkflowStepWizardView(WorkflowObjectMixin, View):
    """Present the validator selector in the add-step modal."""

    template_select = "workflows/partials/workflow_step_wizard_select.html"

    def dispatch(self, request, *args, **kwargs):
        if not request.headers.get("HX-Request"):
            return HttpResponse(status=400)
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        step = self._get_step()
        if step is not None:
            edit_url = reverse_with_org(
                "workflows:workflow_step_edit",
                request=request,
                kwargs={"pk": workflow.pk, "step_id": step.pk},
            )
            response = HttpResponse(status=204)
            response["HX-Redirect"] = edit_url
            return response
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

        if stage != "select":
            if step is not None:
                redirect_url = reverse_with_org(
                    "workflows:workflow_step_edit",
                    request=request,
                    kwargs={"pk": workflow.pk, "step_id": step.pk},
                )
            else:
                redirect_url = reverse_with_org(
                    "workflows:workflow_detail",
                    request=request,
                    kwargs={"pk": workflow.pk},
                )
            response = HttpResponse(status=204)
            response["HX-Redirect"] = redirect_url
            return response

        validators = self._available_validators(workflow)
        action_definitions = self._available_action_definitions()
        tabs, options = self._build_step_tabs(validators, action_definitions)
        form = WorkflowStepTypeForm(request.POST, options=options)
        if form.is_valid():
            if workflow.steps.count() >= MAX_STEP_COUNT:
                message = _("You can add up to %(count)s steps per workflow.") % {
                    "count": MAX_STEP_COUNT,
                }
                return _hx_trigger_response(message, level="warning", status_code=409)
            selection = form.get_selection()
            if selection["kind"] == "validator":
                validator = selection["object"]
                create_url = reverse_with_org(
                    "workflows:workflow_step_create",
                    request=request,
                    kwargs={"pk": workflow.pk, "validator_id": validator.pk},
                )
            else:
                definition: ActionDefinition = selection["object"]
                create_url = reverse_with_org(
                    "workflows:workflow_step_action_create",
                    request=request,
                    kwargs={
                        "pk": workflow.pk,
                        "action_definition_id": definition.pk,
                    },
                )
            response = HttpResponse(status=204)
            response["HX-Redirect"] = create_url
            response["HX-Trigger"] = json.dumps(
                {
                    "close-modal": "workflowStepModal",
                },
            )
            return response
        return self._render_select(request, workflow, form=form)

    # Helper methods ---------------------------------------------------------

    def _get_step(self) -> WorkflowStep | None:
        step_id = self.kwargs.get("step_id")
        if not step_id:
            return None
        workflow = self.get_workflow()
        return get_object_or_404(WorkflowStep, workflow=workflow, pk=step_id)

    def _available_validators(self, workflow: Workflow) -> list[Validator]:
        qs = Validator.objects.filter(
            models.Q(org__isnull=True) | models.Q(org=workflow.org)
        )
        return list(
            qs.order_by("validation_type", "name", "pk"),
        )

    def _available_action_definitions(self) -> list[ActionDefinition]:
        return list(
            ActionDefinition.objects.filter(is_active=True).order_by(
                "action_category", "name"
            )
        )

    def _render_select(self, request, workflow: Workflow, form=None, status=200):
        validators = self._available_validators(workflow)
        action_definitions = self._available_action_definitions()

        tabs, options = self._build_step_tabs(validators, action_definitions)

        selected_value = None
        if form is not None:
            selected_value = form.data.get("choice") or form.initial.get("choice")
        else:
            selected_value = request.GET.get("selected")

        selected_tab = self._resolve_selected_tab(tabs, selected_value)
        form = form or WorkflowStepTypeForm(options=options)

        context = {
            "workflow": workflow,
            "form": form,
            "validator_tabs": tabs,
            "selected_tab": selected_tab,
            "max_step_count": MAX_STEP_COUNT,
            "step": None,
            "limit_reached": False,
            "selected_value": str(selected_value) if selected_value else None,
        }
        return render(request, self.template_select, context, status=status)

    def _build_step_tabs(
        self,
        validators: list[Validator],
        action_definitions: list[ActionDefinition],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        tabs: list[dict[str, object]] = []
        options: list[dict[str, object]] = []

        validator_groups: list[tuple[str, str, set[str] | None]] = [
            (
                "basic",
                str(_("Validators")),
                {
                    ValidationType.BASIC,
                    ValidationType.JSON_SCHEMA,
                    ValidationType.XML_SCHEMA,
                },
            ),
            (
                "advanced",
                str(_("Advanced Validators")),
                {
                    ValidationType.AI_ASSIST,
                    ValidationType.ENERGYPLUS,
                },
            ),
        ]

        handled: list[Validator] = []
        for slug, label, types in validator_groups:
            if types:
                filtered = [
                    v
                    for v in validators
                    if v.validation_type in types and v not in handled
                ]
                handled.extend(filtered)
            else:
                filtered = []
            members = [self._serialize_validator(v) for v in filtered]
            tabs.append({"slug": slug, "label": label, "entries": members})
            options.extend(members)

        remaining_validators = [v for v in validators if v not in handled]
        if remaining_validators:
            advanced_tab = next(
                (tab for tab in tabs if tab["slug"] == "advanced"),
                None,
            )
            if advanced_tab is not None:
                serialized = [
                    self._serialize_validator(v) for v in remaining_validators
                ]
                advanced_tab["entries"].extend(serialized)
                options.extend(serialized)

        integration_entries = [
            self._serialize_action_definition(defn)
            for defn in action_definitions
            if defn.action_category == ActionCategoryType.INTEGRATION
        ]
        certification_entries = [
            self._serialize_action_definition(defn)
            for defn in action_definitions
            if defn.action_category == ActionCategoryType.CERTIFICATION
        ]

        tabs.append(
            {
                "slug": "integrations",
                "label": str(_("Integrations")),
                "entries": integration_entries,
            },
        )
        tabs.append(
            {
                "slug": "certifications",
                "label": str(_("Certifications")),
                "entries": certification_entries,
            },
        )
        options.extend(integration_entries)
        options.extend(certification_entries)

        return tabs, options

    def _serialize_validator(self, validator: Validator) -> dict[str, object]:
        return {
            "value": f"validator:{validator.pk}",
            "label": validator.name,
            "name": validator.name,
            "subtitle": validator.get_validation_type_display(),
            "description": validator.description,
            "icon": getattr(validator, "display_icon", "bi-sliders"),
            "kind": "validator",
            "object": validator,
        }

    def _serialize_action_definition(
        self,
        definition: ActionDefinition,
    ) -> dict[str, object]:
        return {
            "value": f"action:{definition.pk}",
            "label": definition.name,
            "name": definition.name,
            "subtitle": definition.get_action_category_display(),
            "description": definition.description,
            "icon": definition.icon or "bi-gear",
            "kind": "action",
            "object": definition,
        }

    def _resolve_selected_tab(
        self,
        tabs: list[dict[str, object]],
        selected_value: str | None,
    ) -> str:
        if selected_value:
            for tab in tabs:
                for entry in tab["entries"]:
                    if str(entry["value"]) == str(selected_value):
                        return tab["slug"]
        for tab in tabs:
            if tab["entries"]:
                return tab["slug"]
        return tabs[0]["slug"] if tabs else "basic"


class WorkflowStepFormView(WorkflowObjectMixin, FormView):
    """Render the full-screen workflow step editor for create/update."""

    template_name = "workflows/workflow_step_form.html"
    mode: str = "create"
    validator_url_kwarg = "validator_id"
    action_definition_url_kwarg = "action_definition_id"
    step_url_kwarg = "step_id"
    saved_step: WorkflowStep | None = None

    def dispatch(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        if not self.user_can_manage_workflow():
            return HttpResponse(status=403)
        if self.mode == "create" and workflow.steps.count() >= MAX_STEP_COUNT:
            messages.warning(
                request,
                _("You can add up to %(count)s steps per workflow.")
                % {
                    "count": MAX_STEP_COUNT,
                },
            )
            detail_url = reverse_with_org(
                "workflows:workflow_detail",
                request=request,
                kwargs={"pk": workflow.pk},
            )
            return HttpResponseRedirect(detail_url)
        return super().dispatch(request, *args, **kwargs)

    def get_step(self) -> WorkflowStep | None:
        if self.mode != "update":
            return None
        if not hasattr(self, "_step"):
            workflow = self.get_workflow()
            step_id = self.kwargs.get(self.step_url_kwarg)
            self._step = get_object_or_404(
                WorkflowStep,
                workflow=workflow,
                pk=step_id,
            )
        return getattr(self, "_step", None)

    def _validator_queryset(self):
        workflow = self.get_workflow()
        return Validator.objects.filter(
            Q(is_system=True) | Q(org=workflow.org),
        )

    def get_validator(self) -> Validator:
        if self.is_action_step():
            raise Http404
        if not hasattr(self, "_validator"):
            if self.mode == "update":
                step = self.get_step()
                if step is None:
                    raise Http404
                self._validator = step.validator
            else:
                validator_id = self.kwargs.get(self.validator_url_kwarg)
                self._validator = get_object_or_404(self._validator_queryset(), pk=validator_id)
        return self._validator

    def get_action_definition(self) -> ActionDefinition:
        if not hasattr(self, "_action_definition"):
            if self.mode == "update":
                step = self.get_step()
                if step is None or not step.action:
                    raise Http404
                self._action_definition = step.action.definition
            else:
                definition_id = self.kwargs.get(self.action_definition_url_kwarg)
                self._action_definition = get_object_or_404(
                    ActionDefinition,
                    pk=definition_id,
                    is_active=True,
                )
        return self._action_definition

    def is_action_step(self) -> bool:
        if self.mode == "update":
            step = self.get_step()
            return bool(step and step.action_id)
        return bool(self.kwargs.get(self.action_definition_url_kwarg))

    def get_form_class(self):
        if self.is_action_step():
            definition = self.get_action_definition()
            form_class = get_action_form(definition.type)
            if form_class is None:
                raise Http404("Unsupported action type.")
            return form_class
        validator = self.get_validator()
        return get_config_form_class(validator.validation_type)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["step"] = self.get_step()
        if self.is_action_step():
            kwargs["definition"] = self.get_action_definition()
        return kwargs

    def form_valid(self, form):
        workflow = self.get_workflow()
        if self.is_action_step():
            definition = self.get_action_definition()
            saved_step = save_workflow_action_step(
                workflow,
                definition,
                form,
                step=self.get_step(),
            )
        else:
            validator = self.get_validator()
            saved_step = save_workflow_step(
                workflow,
                validator,
                form,
                step=self.get_step(),
            )
        _resequence_workflow_steps(workflow)
        self.saved_step = saved_step
        if self.mode == "create":
            message = _("Workflow step added.")
        else:
            message = _("Workflow step updated.")
        messages.success(self.request, message)
        return HttpResponseRedirect(self.get_success_url())

    def form_invalid(self, form):
        return self.render_to_response(
            self.get_context_data(form=form),
            status=400,
        )

    def get_success_url(self):
        workflow = self.get_workflow()
        if hasattr(self, "saved_step") and self.saved_step:
            anchor = (
                "#workflow-step-assertions"
                if self.saved_step.validator
                and self.saved_step.validator.validation_type
                in ADVANCED_VALIDATION_TYPES
                else "#workflow-step-details"
            )
            return (
                reverse_with_org(
                    "workflows:workflow_step_edit",
                    request=self.request,
                    kwargs={"pk": workflow.pk, "step_id": self.saved_step.pk},
                )
                + anchor
            )
        detail_url = reverse_with_org(
            "workflows:workflow_detail",
            request=self.request,
            kwargs={"pk": workflow.pk},
        )
        return f"{detail_url}#workflow-steps-col"

    def get_neighbor_steps(self) -> tuple[WorkflowStep | None, WorkflowStep | None]:
        step = self.get_step()
        if step is None:
            return (None, None)
        steps = list(self.get_workflow().steps.all().order_by("order", "pk"))
        previous_step = None
        next_step = None
        for index, current in enumerate(steps):
            if current.pk == step.pk:
                if index > 0:
                    previous_step = steps[index - 1]
                if index < len(steps) - 1:
                    next_step = steps[index + 1]
                break
        return previous_step, next_step

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = self.get_workflow()
        step = self.get_step()
        details: dict[str, object]
        icon = "bi-sliders"
        if self.is_action_step():
            definition = self.get_action_definition()
            icon = definition.icon or icon
            details = {
                "name": definition.name,
                "description": definition.description,
                "type_label": definition.get_action_category_display(),
                "icon": icon,
            }
        else:
            validator = self.get_validator()
            icon = getattr(validator, "display_icon", icon)
            details = {
                "name": validator.name,
                "description": validator.description,
                "type_label": validator.get_validation_type_display(),
                "icon": icon,
            }
        prev_step, next_step = self.get_neighbor_steps()
        context.update(
            {
                "workflow": workflow,
                "step": step,
                "subject_details": details,
                "validator_details": details,
                "is_action_step": self.is_action_step(),
                "is_create": self.mode == "create",
                "max_step_count": MAX_STEP_COUNT,
                "previous_step": prev_step,
                "next_step": next_step,
                "steps_count": workflow.steps.count(),
                "show_assertion_link": bool(
                    not self.is_action_step()
                    and step
                    and step.validator
                    and step.validator.validation_type in ADVANCED_VALIDATION_TYPES
                ),
            },
        )
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
        if self.mode == "create":
            breadcrumbs.append({"name": _("Add step"), "url": ""})
        else:
            step = self.get_step()
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
            step_url = reverse_with_org(
                "workflows:workflow_step_edit",
                request=self.request,
                kwargs={"pk": workflow.pk, "step_id": step.pk if step else ""},
            )

            breadcrumbs.append(
                {
                    "name": step.step_number_display,
                    "url": step_url,
                },
            )
            breadcrumbs.append({"name": _("Edit Step Detail"), "url": ""})
        return breadcrumbs


class WorkflowStepEditView(WorkflowObjectMixin, TemplateView):
    """Two-column overview for validator-based steps."""

    template_name = "workflows/workflow_step_detail.html"

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            return HttpResponse(status=403)
        self.step = get_object_or_404(
            WorkflowStep,
            workflow=self.get_workflow(),
            pk=self.kwargs.get("step_id"),
        )
        if self.step.action_id:
            return HttpResponseRedirect(
                reverse_with_org(
                    "workflows:workflow_step_settings",
                    request=request,
                    kwargs={
                        "pk": self.get_workflow().pk,
                        "step_id": self.step.pk,
                    },
                )
            )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = self.get_workflow()
        validator = self.step.validator
        ruleset = None
        assertions = []
        catalog_entries = []
        allow_assertions = (
            validator and validator.validation_type in ADVANCED_VALIDATION_TYPES
        )
        if allow_assertions:
            ruleset = self.step.ruleset or _ensure_advanced_ruleset(
                workflow, self.step, validator
            )
            assertions = list(ruleset.assertions.all().order_by("order", "pk"))
        if validator:
            catalog_entries = list(
                validator.catalog_entries.order_by("entry_type", "order", "slug")
            )
        context.update(
            {
                "workflow": workflow,
                "step": self.step,
                "validator": validator,
                "assertions": assertions,
                "ruleset": ruleset,
                "can_manage_assertions": self.user_can_manage_workflow()
                and allow_assertions,
                "supports_assertions": allow_assertions,
                "catalog_entries": catalog_entries,
            },
        )
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
        breadcrumbs.append(
            {
                "name": self.step.step_number_display,
                "url": reverse_with_org(
                    "workflows:workflow_step_edit",
                    request=self.request,
                    kwargs={"pk": workflow.pk, "step_id": self.step.pk},
                ),
            },
        )
        return breadcrumbs


class WorkflowStepCreateView(WorkflowStepFormView):
    """Create a new workflow step for the given validator."""

    mode = "create"


class WorkflowActionStepCreateView(WorkflowStepFormView):
    """Create a new workflow step for the selected action definition."""

    mode = "create"


class WorkflowStepUpdateView(WorkflowStepFormView):
    """Edit an existing workflow step in full-page mode."""

    mode = "update"


class WorkflowStepDeleteView(WorkflowObjectMixin, View):
    def post(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        if not self.user_can_manage_workflow():
            return HttpResponse(status=403)
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
        if not self.user_can_manage_workflow():
            return HttpResponse(status=403)
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


class WorkflowStepAssertionsMixin(WorkflowObjectMixin):
    """Shared helpers for assertion management views."""

    def dispatch(self, request, *args, **kwargs):
        self.step = get_object_or_404(
            WorkflowStep,
            workflow=self.get_workflow(),
            pk=self.kwargs.get("step_id"),
        )
        if not self._supports_assertions():
            messages.error(
                request,
                _("Assertions are only available for advanced validators."),
            )
            return HttpResponseRedirect(
                reverse_with_org(
                    "workflows:workflow_detail",
                    request=request,
                    kwargs={"pk": self.get_workflow().pk},
                ),
            )
        return super().dispatch(request, *args, **kwargs)

    def _supports_assertions(self) -> bool:
        validator = getattr(self.step, "validator", None)
        if not validator:
            return False
        return validator.validation_type in ADVANCED_VALIDATION_TYPES

    def get_ruleset(self) -> Ruleset:
        validator = self.step.validator
        ruleset = getattr(self.step, "ruleset", None)
        if ruleset is None and validator is not None:
            ruleset = _ensure_advanced_ruleset(
                self.get_workflow(),
                self.step,
                validator,
            )
        return ruleset

    def get_catalog_choices(self):
        if hasattr(self, "_catalog_choice_cache"):
            return self._catalog_choice_cache
        validator = self.step.validator
        choices: list[tuple[str, str]] = []
        entries = []
        if validator:
            entries = list(validator.catalog_entries.order_by("order", "slug"))
            for entry in entries:
                label = f"{entry.label} ({entry.slug})"
                choices.append((entry.slug, label))
        self._catalog_entries_cache = entries
        self._catalog_choice_cache = choices
        return choices

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = self.get_workflow()
        context.update(
            {
                "workflow": workflow,
                "step": self.step,
                "validator": self.step.validator,
                "ruleset": self.get_ruleset(),
                "assertions": self.get_ruleset()
                .assertions.all()
                .order_by("order", "pk"),
                "can_manage_assertions": self.user_can_manage_workflow(),
            },
        )
        return context

    def get_breadcrumbs(self):
        breadcrumbs = super().get_breadcrumbs()
        workflow = self.get_workflow()
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
        breadcrumbs.append({"name": _("Assertions"), "url": ""})
        return breadcrumbs


class WorkflowStepAssertionModalBase(WorkflowStepAssertionsMixin, FormView):
    template_name = "workflows/partials/assertion_form.html"
    form_class = RulesetAssertionForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["catalog_choices"] = self.get_catalog_choices()
        kwargs["catalog_entries"] = getattr(self, "_catalog_entries_cache", [])
        kwargs["validator"] = self.step.validator
        kwargs["target_slug_datalist_id"] = self.get_target_slug_datalist_id()
        return kwargs

    def get_target_slug_datalist_id(self) -> str:
        step_id = getattr(self.step, "pk", "step")
        return f"assertion-target-slug-options-{step_id}"

    def render_to_response(self, context, **response_kwargs):
        if self.request.headers.get("HX-Request"):
            return render(
                self.request,
                self.template_name,
                context,
                status=response_kwargs.get("status", 200),
            )
        return super().render_to_response(context, **response_kwargs)

    def get_success_url(self):
        return (
            reverse_with_org(
                "workflows:workflow_step_edit",
                request=self.request,
                kwargs={
                    "pk": self.get_workflow().pk,
                    "step_id": self.step.pk,
                },
            )
            + "#workflow-step-assertions"
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "modal_title": getattr(self, "modal_title", _("Assertion")),
                "form_action": self.request.path,
                "submit_label": getattr(self, "submit_label", _("Save")),
                "target_slug_datalist_id": self.get_target_slug_datalist_id(),
                "catalog_choices": self.get_catalog_choices(),
                "allow_custom_targets": bool(
                    getattr(self.step.validator, "allow_custom_assertion_targets", False)
                ),
            }
        )
        return context


class WorkflowStepAssertionCreateView(WorkflowStepAssertionModalBase):
    modal_title = _("Add Assertion")
    submit_label = _("Add Assertion")

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        ruleset = self.get_ruleset()
        max_order = (
            ruleset.assertions.aggregate(max_order=models.Max("order"))["max_order"]
            or 0
        )
        RulesetAssertion.objects.create(
            ruleset=ruleset,
            order=max_order + 10,
            assertion_type=form.cleaned_data["assertion_type"],
            operator=form.cleaned_data["resolved_operator"],
            target_catalog=form.cleaned_data.get("target_catalog_entry"),
            target_field=form.cleaned_data.get("target_field_value") or "",
            severity=form.cleaned_data["severity"],
            when_expression=form.cleaned_data.get("when_expression") or "",
            rhs=form.cleaned_data["rhs_payload"],
            options=form.cleaned_data["options_payload"],
            message_template=form.cleaned_data.get("message_template") or "",
            cel_cache=form.cleaned_data.get("cel_cache") or "",
        )
        messages.success(self.request, _("Assertion added."))
        return _hx_trigger_response(
            message=_("Assertion added."),
            close_modal="workflowAssertionModal",
            extra_payload={"assertions-changed": True},
        )


class WorkflowStepAssertionUpdateView(WorkflowStepAssertionModalBase):
    modal_title = _("Edit Assertion")
    submit_label = _("Save changes")

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def _get_assertion(self) -> RulesetAssertion:
        if not hasattr(self, "_assertion"):
            self._assertion = get_object_or_404(
                RulesetAssertion,
                pk=self.kwargs.get("assertion_id"),
                ruleset=self.get_ruleset(),
            )
        return self._assertion

    def get_initial(self):
        return RulesetAssertionForm.initial_from_instance(self._get_assertion())

    def form_valid(self, form):
        assertion = self._get_assertion()
        RulesetAssertion.objects.filter(pk=assertion.pk).update(
            assertion_type=form.cleaned_data["assertion_type"],
            operator=form.cleaned_data["resolved_operator"],
            target_catalog=form.cleaned_data.get("target_catalog_entry"),
            target_field=form.cleaned_data.get("target_field_value") or "",
            severity=form.cleaned_data["severity"],
            when_expression=form.cleaned_data.get("when_expression") or "",
            rhs=form.cleaned_data["rhs_payload"],
            options=form.cleaned_data["options_payload"],
            message_template=form.cleaned_data.get("message_template") or "",
            cel_cache=form.cleaned_data.get("cel_cache") or "",
        )
        messages.success(self.request, _("Assertion updated."))
        return _hx_trigger_response(
            message=_("Assertion updated."),
            close_modal="workflowAssertionModal",
            extra_payload={"assertions-changed": True},
        )


class WorkflowStepAssertionDeleteView(WorkflowStepAssertionsMixin, View):
    def post(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        ruleset = self.get_ruleset()
        assertion = get_object_or_404(
            RulesetAssertion,
            pk=self.kwargs.get("assertion_id"),
            ruleset=ruleset,
        )
        assertion.delete()
        messages.success(request, _("Assertion removed."))
        if request.headers.get("HX-Request"):
            return _hx_trigger_response(
                message=_("Assertion removed."),
                close_modal="workflowAssertionModal",
                extra_payload={"assertions-changed": True},
            )
        return HttpResponseRedirect(
            reverse_with_org(
                "workflows:workflow_step_edit",
                request=request,
                kwargs={
                    "pk": self.get_workflow().pk,
                    "step_id": self.step.pk,
                },
            )
        )


class WorkflowStepAssertionMoveView(WorkflowStepAssertionsMixin, View):
    def post(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        ruleset = self.get_ruleset()
        assertion = get_object_or_404(
            RulesetAssertion,
            pk=self.kwargs.get("assertion_id"),
            ruleset=ruleset,
        )
        direction = request.POST.get("direction")
        assertions = list(ruleset.assertions.order_by("order", "pk"))
        try:
            index = assertions.index(assertion)
        except ValueError:
            return _hx_trigger_response(
                status_code=400, message=_("Assertion not found.")
            )
        if direction == "up" and index > 0:
            assertions[index - 1], assertions[index] = (
                assertions[index],
                assertions[index - 1],
            )
        elif direction == "down" and index < len(assertions) - 1:
            assertions[index], assertions[index + 1] = (
                assertions[index + 1],
                assertions[index],
            )
        else:
            return _hx_trigger_response(status_code=204)
        with transaction.atomic():
            for pos, item in enumerate(assertions, start=1):
                RulesetAssertion.objects.filter(pk=item.pk).update(order=pos * 10)
        return _hx_redirect_response(
            reverse_with_org(
                "workflows:workflow_step_edit",
                request=request,
                kwargs={
                    "pk": self.get_workflow().pk,
                    "step_id": self.step.pk,
                },
            )
            + "#workflow-step-assertions"
        )
