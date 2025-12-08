import json
import logging
import time
from http import HTTPStatus

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError
from django.db import models
from django.db import transaction
from django.db.models import Count
from django.db.models import Q
from django.http import Http404
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse
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
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied as DRFPermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response as APIResponse

from validibot.actions.constants import ActionCategoryType
from validibot.actions.models import ActionDefinition
from validibot.actions.models import SignedCertificateAction
from validibot.actions.models import SlackMessageAction
from validibot.actions.registry import get_action_form
from validibot.core.utils import pretty_json
from validibot.core.utils import pretty_xml
from validibot.core.utils import reverse_with_org
from validibot.core.view_helpers import hx_redirect_response
from validibot.core.view_helpers import hx_trigger_response
from validibot.projects.models import Project
from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.users.mixins import SuperuserRequiredMixin
from validibot.users.models import Organization
from validibot.users.permissions import PermissionCode
from validibot.validations.constants import ADVANCED_VALIDATION_TYPES
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.constants import ValidationType
from validibot.validations.constants import XMLSchemaType
from validibot.validations.forms import RulesetAssertionForm
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import ValidationRun
from validibot.validations.models import Validator
from validibot.validations.serializers import ValidationRunStartSerializer
from validibot.validations.services.validation_run import ValidationRunService
from validibot.workflows.constants import WORKFLOW_LIST_LAYOUT_SESSION_KEY
from validibot.workflows.constants import WORKFLOW_LIST_SHOW_ARCHIVED_SESSION_KEY
from validibot.workflows.constants import WorkflowListLayout
from validibot.workflows.forms import WorkflowPublicInfoForm
from validibot.workflows.forms import WorkflowStepTypeForm
from validibot.workflows.forms import get_config_form_class
from validibot.workflows.mixins import WorkflowAccessMixin
from validibot.workflows.mixins import WorkflowFormViewMixin
from validibot.workflows.mixins import WorkflowLaunchContextMixin
from validibot.workflows.mixins import WorkflowObjectMixin
from validibot.workflows.mixins import WorkflowStepAssertionsMixin
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep
from validibot.workflows.permissions import WorkflowPermission
from validibot.workflows.serializers import WorkflowSerializer
from validibot.workflows.views_helpers import ensure_advanced_ruleset
from validibot.workflows.views_helpers import public_info_card_context
from validibot.workflows.views_helpers import resequence_workflow_steps
from validibot.workflows.views_helpers import resolve_project
from validibot.workflows.views_helpers import save_workflow_action_step
from validibot.workflows.views_helpers import save_workflow_step
from validibot.workflows.views_helpers import user_has_executor_role
from validibot.workflows.views_launch_helpers import LaunchValidationError
from validibot.workflows.views_launch_helpers import build_submission_from_api
from validibot.workflows.views_launch_helpers import build_submission_from_form
from validibot.workflows.views_launch_helpers import launch_api_validation_run
from validibot.workflows.views_launch_helpers import launch_web_validation_run

logger = logging.getLogger(__name__)

MAX_STEP_COUNT = 5


# API Views
# ------------------------------------------------------------------------------


class WorkflowViewSet(viewsets.ModelViewSet):
    queryset = Workflow.objects.all()
    serializer_class = WorkflowSerializer
    permission_classes = [permissions.IsAuthenticated, WorkflowPermission]

    def get_queryset(self):
        # List all workflows the user can access (in any of their orgs)
        return Workflow.objects.for_user(self.request.user)

    def get_serializer_class(self):
        if getattr(self, "action", None) in [
            "start_validation",
        ]:
            return ValidationRunStartSerializer
        return super().get_serializer_class()

    def perform_create(self, serializer):
        org = self._resolve_target_org()
        if not self.request.user.has_perm(
            PermissionCode.WORKFLOW_EDIT.value,
            org,
        ):
            raise DRFPermissionDenied(
                detail=_(
                    "You do not have permission to create "
                    "workflows for this organization.",
                ),
            )
        serializer.save(org=org, user=self.request.user)

    def perform_update(self, serializer):
        workflow: Workflow = self.get_object()
        if not self.request.user.has_perm(
            PermissionCode.WORKFLOW_EDIT.value,
            workflow,
        ):
            raise DRFPermissionDenied(
                detail=_("You do not have permission to update this workflow."),
            )
        requested_org = serializer.validated_data.get("org")
        requested_org_id = self.request.data.get("org")
        if requested_org and requested_org != workflow.org:
            raise DRFValidationError(
                {"org": _("Cannot move a workflow to another organization.")},
            )
        if requested_org_id and str(requested_org_id) != str(workflow.org_id):
            raise DRFValidationError(
                {"org": _("Cannot move a workflow to another organization.")},
            )
        serializer.save(org=workflow.org, user=workflow.user)

    def perform_destroy(self, instance: Workflow):
        if not self.request.user.has_perm(
            PermissionCode.WORKFLOW_EDIT.value,
            instance,
        ):
            raise DRFPermissionDenied(
                detail=_("You do not have permission to delete this workflow."),
            )
        return super().perform_destroy(instance)

    def _resolve_target_org(self) -> Organization:
        org_id = self.request.data.get("org") or getattr(
            self.request.user,
            "current_org_id",
            None,
        )
        if not org_id:
            raise DRFValidationError({"org": _("Organization is required.")})
        try:
            return Organization.objects.get(pk=org_id)
        except Organization.DoesNotExist as exc:
            raise DRFValidationError({"org": _("Organization not found.")}) from exc

    # Public action remains unchanged
    @action(
        detail=True,
        methods=["post"],
        url_path="start",
        url_name="start",
    )
    def start_validation(self, request, pk=None, *args, **kwargs):
        workflow = self.get_object()
        project = resolve_project(workflow=workflow, request=request)
        try:
            submission_build = build_submission_from_api(
                request=request,
                workflow=workflow,
                user=request.user,
                project=project,
                serializer_factory=self.get_serializer,
                multipart_payload=lambda: request.data,
            )
        except LaunchValidationError as exc:
            status_code = exc.status_code
            if status_code == HTTPStatus.FORBIDDEN:
                status_code = HTTPStatus.NOT_FOUND
            return APIResponse(exc.payload, status=status_code)

        return launch_api_validation_run(
            request=request,
            workflow=workflow,
            submission_build=submission_build,
        )


# UI Views
# ------------------------------------------------------------------------------


# UIs for launching and viewing in-process workflows...
# ..............................................................................


class WorkflowLaunchDetailView(WorkflowLaunchContextMixin, TemplateView):
    """Renders and processes the workflow launch form in the web UI."""

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
        breadcrumbs.append({"name": _("Launch Workflow"), "url": ""})
        return breadcrumbs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = self.get_workflow()
        can_execute = user_has_executor_role(self.request.user, workflow)
        has_steps = workflow.steps.exists()
        form = kwargs.get("launch_form")
        if form is None and can_execute and has_steps:
            form = self.get_launch_form(workflow=workflow)
        context.update(
            {
                "workflow": workflow,
                "recent_runs": self.get_recent_runs(workflow),
                "can_execute": can_execute,
                "has_steps": has_steps,
                "launch_form": form,
                "panel_mode": "form",
            },
        )
        return context

    def post(self, request, *args, **kwargs):
        """Handle submission of the workflow launch form."""
        start_time = time.perf_counter()
        workflow = self.get_workflow()
        form = self.get_launch_form(
            workflow=workflow,
            data=request.POST,
            files=request.FILES,
        )

        form_valid = form.is_valid()
        payload_source = form.cleaned_data if form_valid else form.data
        self._remember_launch_input_mode(
            request,
            payload_source.get("payload"),
            mode=request.POST.get("input_mode"),
        )

        if not form_valid:
            context = self.get_context_data(launch_form=form)
            return self.render_to_response(context, status=HTTPStatus.OK)

        # Build the submision ...

        try:
            submission_build = build_submission_from_form(
                request=request,
                workflow=workflow,
                cleaned_data=form.cleaned_data,
            )
            logger.debug(
                "Workflow %s submission built in %.2f ms",
                workflow.pk,
                (time.perf_counter() - start_time) * 1000,
            )
        except ValidationError as exc:
            form.add_error(None, exc.message if hasattr(exc, "message") else str(exc))
            context = self.get_context_data(launch_form=form)
            return self.render_to_response(context, status=HTTPStatus.BAD_REQUEST)
        except LaunchValidationError as exc:
            error_detail = exc.payload.get("detail") or _(
                "Could not run the workflow. Please try again.",
            )
            form.add_error(None, error_detail)
            context = self.get_context_data(launch_form=form)
            return self.render_to_response(context, status=exc.status_code)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception(
                "Failed to prepare submission for workflow run.",
                exc_info=exc,
            )
            form.add_error(
                None,
                _("Something went wrong while preparing the submission."),
            )
            context = self.get_context_data(launch_form=form)
            return self.render_to_response(
                context,
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        # Launch the validation run ...
        response = None
        try:
            launch_result = launch_web_validation_run(
                submission_build=submission_build,
                request=request,
                workflow=workflow,
            )
            response = self.render_run_detail_panel(
                request,
                workflow=workflow,
                run=launch_result.validation_run,
                status_code=launch_result.status or HTTPStatus.CREATED,
            )
            logger.info(
                "Workflow %s launch POST completed in %.2f ms",
                workflow.pk,
                (time.perf_counter() - start_time) * 1000,
            )
        except PermissionError:
            form.add_error(None, _("You do not have permission to run this workflow."))
            context = self.get_context_data(launch_form=form)
            return self.render_to_response(context, status=HTTPStatus.FORBIDDEN)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Run service errored for workflow %s", workflow.pk)
            form.add_error(None, _("Could not run the workflow. Please try again."))
            context = self.get_context_data(launch_form=form)
            return self.render_to_response(
                context,
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        if response:
            return response
        raise RuntimeError("Expected response from launch_web_validation_run")


class WorkflowLaunchStatusView(WorkflowLaunchContextMixin, View):
    http_method_names = ["get"]

    def get(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        run_id = kwargs.get("run_id")
        run = self.load_run_for_display(workflow=workflow, run_id=run_id)
        if run is None:
            raise Http404
        context = {"workflow": workflow}
        context.update(
            self.build_status_area_context(
                workflow=workflow,
                active_run=run,
            ),
        )
        return render(
            request,
            self.status_area_template_name,
            context=context,
        )


class WorkflowLaunchCancelView(WorkflowLaunchContextMixin, View):
    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        if not workflow.can_execute(user=request.user):
            raise PermissionDenied

        run_id = kwargs.get("run_id")
        run = self.load_run_for_display(workflow=workflow, run_id=run_id)
        if run is None:
            raise Http404

        service = ValidationRunService()
        updated_run, cancelled = service.cancel_run(run=run, actor=request.user)

        if cancelled:
            toast_message = _("Workflow validation canceled.")
            toast_level = "success"
        else:
            if updated_run.status == ValidationRunStatus.SUCCEEDED:
                toast_message = _(
                    "Process completed before it could be cancelled.",
                )
            elif updated_run.status == ValidationRunStatus.FAILED:
                toast_message = _(
                    "Process failed before it could be cancelled.",
                )
            else:
                toast_message = _("Unable to cancel this run.")
            toast_level = "info"

        toast_payload = {
            "level": toast_level,
            "message": str(toast_message),
        }

        return self.render_run_detail_panel(
            request,
            workflow=workflow,
            run=updated_run,
            status_code=HTTPStatus.OK,
            toast=toast_payload,
        )


# UIs for public views of workflows...
# ..............................................................................


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


class PublicWorkflowInfoView(DetailView):
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


# UIs for authoring and managing workflows...
# ..............................................................................


class WorkflowListView(WorkflowAccessMixin, ListView):
    template_name = "workflows/workflow_list.html"
    context_object_name = "workflows"
    breadcrumbs = [
        {"name": _("Workflows"), "url": ""},
    ]
    layout_param = "layout"
    default_layout = WorkflowListLayout.GRID
    allowed_layouts = set(WorkflowListLayout.values)
    layout_session_key = WORKFLOW_LIST_LAYOUT_SESSION_KEY

    def get_queryset(self):
        qs = (
            super()
            .get_queryset()
            .annotate(run_count=Count("validation_runs", distinct=True))
        )
        if not self._show_archived():
            qs = qs.filter(is_archived=False)
        search = self.request.GET.get("q", "").strip()
        if search:
            qs = qs.filter(name__icontains=search)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflows: list[Workflow] = list(context["workflows"])
        context["workflows"] = workflows
        context["object_list"] = workflows
        user = self.request.user
        membership = getattr(user, "membership_for_current_org", lambda: None)()
        can_manage = False
        can_execute = False
        can_view = False
        can_toggle_archived = False
        if membership:
            org = membership.org
            can_manage = user.has_perm(PermissionCode.WORKFLOW_EDIT.value, org)
            can_execute = user.has_perm(PermissionCode.WORKFLOW_LAUNCH.value, org)
            can_view = user.has_perm(PermissionCode.WORKFLOW_VIEW.value, org)
            can_toggle_archived = can_manage

        # Attach information about what user can do with each workflow
        # so we don't need to check multiple times in the template
        for wf in workflows:
            wf.curr_user_can_execute = (
                wf.is_active and not wf.is_archived and can_execute
            )
            wf.curr_user_can_delete = self._can_manage_workflow_actions(
                wf,
                self.request.user,
                membership,
            )
            wf.curr_user_can_edit = self._can_manage_workflow_actions(
                wf,
                self.request.user,
                membership,
            )
            wf.curr_user_can_view = can_view
            run_count = getattr(wf, "run_count", None)
            if run_count is None:
                run_count = 1 if wf.validation_runs.exists() else 0
            wf.has_runs = run_count > 0
            wf.run_count = run_count

        layout = str(self._get_layout())
        context.update(
            {
                "search_query": self.request.GET.get("q", ""),
                "current_layout": layout,
                "layout_urls": self._build_layout_urls(),
                "show_archived": self._show_archived(),
                "archived_toggle_urls": self._build_archived_toggle_urls(),
                "can_toggle_archived": can_toggle_archived,
                "can_create_workflow": self.user_can_create_workflow(),
                "can_manage_workflow": self.user_can_manage_workflow(),
                "can_view_workflow": self.user_can_view_workflow(),
                "create_url": reverse_with_org(
                    "workflows:workflow_create",
                    request=self.request,
                ),
            },
        )
        return context

    def _get_layout(self) -> str:
        requested = (self.request.GET.get(self.layout_param) or "").lower()
        if requested in self.allowed_layouts:
            self._remember_layout(requested)
            return requested
        persisted = self.request.session.get(self.layout_session_key)
        if persisted in self.allowed_layouts:
            return persisted
        return self.default_layout

    def _remember_layout(self, layout: str) -> None:
        try:
            self.request.session[self.layout_session_key] = layout
            self.request.session.modified = True
        except Exception:  # pragma: no cover - defensive
            return

    def _build_query_params(self, **overrides) -> str:
        params = self.request.GET.copy()
        params.pop("page", None)
        for key, value in overrides.items():
            if value is None:
                params.pop(key, None)
            else:
                params[key] = value
        return params.urlencode()

    def _build_layout_urls(self) -> dict[str, str]:
        grid_query = self._build_query_params(layout=WorkflowListLayout.GRID)
        table_query = self._build_query_params(layout=WorkflowListLayout.TABLE)
        return {
            "grid": f"?{grid_query}" if grid_query else "?",
            "table": f"?{table_query}" if table_query else "?",
        }

    def _show_archived(self) -> bool:
        if not self._can_toggle_archived():
            return False
        raw = (self.request.GET.get("archived") or "").lower()
        if raw in {"1", "true", "yes"}:
            self._remember_archived(show=True)
            return True
        if raw in {"0", "false", "no"}:
            self._remember_archived(show=False)
            return False
        stored = self.request.session.get(WORKFLOW_LIST_SHOW_ARCHIVED_SESSION_KEY)
        if isinstance(stored, bool):
            return stored
        if isinstance(stored, str):
            return stored.lower() in {"1", "true", "yes"}
        return False

    def _can_toggle_archived(self) -> bool:
        membership = getattr(
            self.request.user,
            "membership_for_current_org",
            lambda: None,
        )()
        if not membership or not getattr(membership, "is_active", False):
            return False
        return self.request.user.has_perm(
            PermissionCode.WORKFLOW_EDIT.value,
            membership.org,
        )

    def _build_archived_toggle_urls(self) -> dict[str, str]:
        base_url = reverse_with_org(
            "workflows:workflow_list",
            request=self.request,
        )
        show_query = self._build_query_params(archived="1")
        hide_query = self._build_query_params(archived="0")
        return {
            "show": f"{base_url}?{show_query}" if show_query else base_url,
            "hide": f"{base_url}?{hide_query}" if hide_query else base_url,
        }

    def _remember_archived(self, *, show: bool) -> None:
        try:
            self.request.session[WORKFLOW_LIST_SHOW_ARCHIVED_SESSION_KEY] = show
            self.request.session.modified = True
        except Exception:  # pragma: no cover - defensive
            return


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
        can_manage_public_info = self.user_can_manage_workflow()
        public_info_context = public_info_card_context(
            self.request,
            workflow,
            can_manage=can_manage_public_info,
        )
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
                "public_info_url": public_info_context["public_info_url"],
                "can_manage_public_info": can_manage_public_info,
                "can_launch_workflow": workflow.can_execute(user=self.request.user),
                "can_manage_workflow": self.user_can_manage_workflow(),
                "can_view_workflow": self.user_can_view_workflow(),
            },
        )
        return context


class WorkflowRunDetailView(WorkflowLaunchContextMixin, TemplateView):
    template_name = "workflows/launch/workflow_run_detail.html"

    def get(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        run = self.load_run_for_display(
            workflow=workflow,
            run_id=self.kwargs.get("run_id"),
        )
        if run is None:
            raise Http404
        return self.render_run_detail_panel(
            request,
            workflow=workflow,
            run=run,
            status_code=HTTPStatus.OK,
        )


class WorkflowLastRunStatusView(
    SuperuserRequiredMixin,
    WorkflowLaunchContextMixin,
    TemplateView,
):
    """
    Displays the most recent run of the workflow. ONLY FOR SUPERUSERS.
    """

    template_name = "workflows/launch/workflow_run_detail.html"

    def get(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        latest_run_id = (
            workflow.validation_runs.order_by("-created")
            .values_list("pk", flat=True)
            .first()
        )
        if not latest_run_id:
            messages.info(
                request,
                _("This workflow has not run yet. Launch it to see run details."),
            )
            return HttpResponseRedirect(
                reverse_with_org(
                    "workflows:workflow_launch",
                    request=request,
                    kwargs={"pk": workflow.pk},
                ),
            )
        run = self.load_run_for_display(workflow=workflow, run_id=latest_run_id)
        if run is None:
            raise Http404
        return self.render_run_detail_panel(
            request,
            workflow=workflow,
            run=run,
            status_code=HTTPStatus.OK,
        )


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

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

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


class WorkflowArchiveView(WorkflowObjectMixin, View):
    """Archive a workflow (set inactive) without deleting historical runs."""

    def post(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        membership = self.request.user.membership_for_current_org()
        show_archived = self._determine_show_archived(request)
        if not self._can_manage_workflow_actions(
            workflow,
            self.request.user,
            membership,
        ):
            return HttpResponse(status=403)

        unarchive = (request.POST.get("unarchive") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }

        if unarchive:
            if not workflow.is_archived:
                messages.info(request, _("Workflow is already active."))
            else:
                workflow.is_archived = False
                workflow.is_active = True
                workflow.save(update_fields=["is_archived", "is_active"])
                messages.success(
                    request,
                    _("Workflow unarchived and re-enabled for new runs."),
                )
        elif workflow.is_archived:
            messages.info(request, _("Workflow is already archived."))
        else:
            workflow.is_archived = True
            workflow.is_active = False
            workflow.save(update_fields=["is_archived", "is_active"])
            messages.info(
                request,
                _("Workflow archived and disabled. Runs remain available for audit."),
            )

        if request.headers.get("HX-Request"):
            layout = self._determine_layout(request)
            # When archiving and archived items are hidden, remove the row.
            if not show_archived and workflow.is_archived:
                return HttpResponse("", status=204)

            self._populate_workflow_metadata(workflow)
            self._attach_permissions(workflow)
            template = (
                "workflows/partials/components/workflow_table_row.html"
                if layout == WorkflowListLayout.TABLE
                else "workflows/partials/components/workflow_grid_item.html"
            )
            html = render_to_string(
                template,
                {
                    "workflow": workflow,
                    "show_archived": show_archived,
                    "current_layout": layout,
                },
                request=request,
            )
            response = HttpResponse(html)
            response["HX-Trigger"] = "workflowArchived"
            return response

        success_url = reverse_with_org(
            "workflows:workflow_list",
            request=request,
        )
        return HttpResponseRedirect(success_url)

    def _determine_show_archived(self, request) -> bool:
        raw = (request.POST.get("show_archived") or "").lower()
        if raw in {"1", "true", "yes"}:
            self._remember_archived(request, show=True)
            return True
        if raw in {"0", "false", "no"}:
            self._remember_archived(request, show=False)
            return False
        stored = request.session.get(WORKFLOW_LIST_SHOW_ARCHIVED_SESSION_KEY)
        if isinstance(stored, bool):
            return stored
        if isinstance(stored, str):
            return stored.lower() in {"1", "true", "yes"}
        return False

    def _remember_archived(self, request, *, show: bool) -> None:
        try:
            request.session[WORKFLOW_LIST_SHOW_ARCHIVED_SESSION_KEY] = show
            request.session.modified = True
        except Exception:  # pragma: no cover - defensive
            return

    def _determine_layout(self, request) -> str:
        layout = (request.POST.get("layout") or "").lower()
        if layout in WorkflowListLayout.values:
            return layout
        stored = request.session.get(WORKFLOW_LIST_LAYOUT_SESSION_KEY)
        if stored in WorkflowListLayout.values:
            return stored
        return WorkflowListLayout.GRID

    def _populate_workflow_metadata(self, workflow: Workflow) -> None:
        run_count = getattr(workflow, "run_count", None)
        if run_count is None:
            run_count = 1 if workflow.validation_runs.exists() else 0
        workflow.has_runs = run_count > 0
        workflow.run_count = run_count

    def _attach_permissions(self, workflow: Workflow) -> None:
        """
        Recompute per-user permission flags for a workflow when rendering partials.
        """
        membership = getattr(
            self.request.user, "membership_for_current_org", lambda: None
        )()
        can_execute = False
        can_view = False
        if membership and getattr(membership, "is_active", False):
            org = membership.org
            can_execute = self.request.user.has_perm(
                PermissionCode.WORKFLOW_LAUNCH.value,
                org,
            )
            can_view = self.request.user.has_perm(
                PermissionCode.WORKFLOW_VIEW.value,
                org,
            )
        workflow.curr_user_can_execute = (
            workflow.is_active and not workflow.is_archived and can_execute
        )
        workflow.curr_user_can_delete = self._can_manage_workflow_actions(
            workflow,
            self.request.user,
            membership,
        )
        workflow.curr_user_can_edit = workflow.curr_user_can_delete
        workflow.curr_user_can_view = can_view


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
        workflow = self.get_workflow()
        desired_visibility = bool(form.cleaned_data.get("make_info_public"))
        if workflow.make_info_public != desired_visibility:
            workflow.make_info_public = desired_visibility
            workflow.save(update_fields=["make_info_public"])
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
        context.update(
            {
                "workflow": workflow,
                "can_manage_public_info": self.user_can_manage_workflow(),
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


class WorkflowPublicVisibilityUpdateView(WorkflowObjectMixin, View):
    """Toggle whether the workflow's public info page is visible."""

    def post(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            return HttpResponse(status=403)
        workflow = self.get_workflow()
        raw_state = (request.POST.get("make_info_public") or "").strip().lower()
        if raw_state in {"true", "1", "on"}:
            new_state = True
        elif raw_state in {"false", "0", "off"}:
            new_state = False
        else:
            new_state = not workflow.make_info_public

        if workflow.make_info_public != new_state:
            workflow.make_info_public = new_state
            workflow.save(update_fields=["make_info_public"])

        context = public_info_card_context(
            request,
            workflow,
            can_manage=self.user_can_manage_workflow(),
        )
        html = render_to_string(
            "workflows/partials/workflow_public_info_card.html",
            context,
            request=request,
        )
        return HttpResponse(html)


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
                                schema_type,
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
            "can_view_workflow": self.user_can_view_workflow(),
            "can_manage_workflow": self.user_can_manage_workflow(),
            "can_launch_workflow": workflow.can_execute(user=request.user),
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
        tabs, options = self._build_step_tabs(
            workflow,
            validators,
            action_definitions,
        )
        form = WorkflowStepTypeForm(request.POST, options=options)
        if form.is_valid():
            if workflow.steps.count() >= MAX_STEP_COUNT:
                message = _("You can add up to %(count)s steps per workflow.") % {
                    "count": MAX_STEP_COUNT,
                }
                return hx_trigger_response(message, level="warning", status_code=409)
            selection = form.get_selection()
            if selection["kind"] == "validator":
                validator = selection["object"]
                if not workflow.validator_is_compatible(validator):
                    allowed = ", ".join(workflow.allowed_file_type_labels())
                    form.add_error(
                        None,
                        _(
                            "%(validator)s cannot be added because this workflow only "
                            "accepts %(allowed)s submissions.",
                        )
                        % {
                            "validator": validator.name,
                            "allowed": allowed or _("the selected"),
                        },
                    )
                    return self._render_select(
                        request,
                        workflow,
                        form=form,
                        status=400,
                    )
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
        """
        Return validators visible to this workflow's org. Compatibility is
        enforced at save time so the selector can still show validators that
        would require different file types.
        """
        validators: list[Validator] = []
        for validator in Validator.objects.filter(
            models.Q(org__isnull=True) | models.Q(org=workflow.org),
        ).order_by("validation_type", "name", "pk"):
            self._ensure_validator_defaults(validator)
            validators.append(validator)
        return validators

    def _available_action_definitions(self) -> list[ActionDefinition]:
        return list(
            ActionDefinition.objects.filter(is_active=True).order_by(
                "action_category",
                "name",
            ),
        )

    def _render_select(self, request, workflow: Workflow, form=None, status=200):
        validators = self._available_validators(workflow)
        action_definitions = self._available_action_definitions()

        tabs, options = self._build_step_tabs(
            workflow,
            validators,
            action_definitions,
        )

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
        workflow: Workflow,
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
                    ValidationType.FMI,
                },
            ),
            (
                "custom",
                str(_("Custom Validators")),
                {
                    ValidationType.CUSTOM_VALIDATOR,
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
            members = [self._serialize_validator(workflow, v) for v in filtered]
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
                    self._serialize_validator(workflow, v) for v in remaining_validators
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

    def _ensure_validator_defaults(self, validator: Validator) -> None:
        """
        Backfill expected supported formats/file types for validators created
        before defaults expanded (notably FMI, which now accepts JSON/TEXT).
        """
        if validator.validation_type != ValidationType.FMI:
            return
        changed = False
        if validator.supported_file_types is None:
            validator.supported_file_types = []
            changed = True
        if validator.supported_data_formats is None:
            validator.supported_data_formats = []
            changed = True
        for ft in (SubmissionFileType.JSON, SubmissionFileType.TEXT):
            if ft not in validator.supported_file_types:
                validator.supported_file_types.append(ft)
                changed = True
        for fmt in (SubmissionDataFormat.JSON, SubmissionDataFormat.TEXT):
            if fmt not in validator.supported_data_formats:
                validator.supported_data_formats.append(fmt)
                changed = True
        if changed:
            validator.save(
                update_fields=["supported_file_types", "supported_data_formats"],
            )

    def _serialize_validator(
        self,
        workflow: Workflow,
        validator: Validator,
    ) -> dict[str, object]:
        is_compatible = workflow.validator_is_compatible(validator)
        allowed = ", ".join(workflow.allowed_file_type_labels())
        disabled_reason = None
        if not is_compatible:
            disabled_reason = _(
                "Not allowed for this workflow's submission types (%(allowed)s).",
            ) % {"allowed": allowed or _("selected types")}
        return {
            "value": f"validator:{validator.pk}",
            "label": validator.name,
            "name": validator.name,
            "subtitle": validator.get_validation_type_display(),
            "description": validator.description,
            "short_description": validator.short_description,
            "icon": getattr(validator, "display_icon", "bi-sliders"),
            "kind": "validator",
            "object": validator,
            "disabled": not is_compatible,
            "disabled_reason": disabled_reason,
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
                self._validator = get_object_or_404(
                    self._validator_queryset(),
                    pk=validator_id,
                )
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
            if not workflow.validator_is_compatible(validator):
                allowed = ", ".join(workflow.allowed_file_type_labels())
                form.add_error(
                    None,
                    _(
                        "%(validator)s cannot be added because this workflow only "
                        "accepts %(allowed)s submissions.",
                    )
                    % {
                        "validator": validator.name,
                        "allowed": allowed or _("the selected"),
                    },
                )
                return self.form_invalid(form)
            saved_step = save_workflow_step(
                workflow,
                validator,
                form,
                step=self.get_step(),
            )
        resequence_workflow_steps(workflow)
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
                "short_description": validator.short_description,
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
                    and step.validator.validation_type in ADVANCED_VALIDATION_TYPES,
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
                ),
            )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = self.get_workflow()
        validator = self.step.validator
        ruleset = None
        assertions = []
        catalog_entries = []
        catalog_display = validator.catalog_display if validator else None
        allow_assertions = (
            validator and validator.validation_type in ADVANCED_VALIDATION_TYPES
        )
        if allow_assertions:
            ruleset = self.step.ruleset or ensure_advanced_ruleset(
                workflow,
                self.step,
                validator,
            )
            assertions = list(ruleset.assertions.all().order_by("order", "pk"))
        if validator:
            catalog_entries = list(
                validator.catalog_entries.order_by(
                    "entry_type",
                    "run_stage",
                    "order",
                    "slug",
                ),
            )
        grouped_assertions = {
            "input": [],
            "output": [],
        }
        for assertion in assertions:
            stage = assertion.resolved_run_stage
            key = "input" if stage == CatalogRunStage.INPUT else "output"
            grouped_assertions[key].append(assertion)
        uses_signal_stages = bool(
            validator and validator.has_signal_stages() and allow_assertions,
        )
        default_assertions_count = validator.rules.count() if validator else 0
        context.update(
            {
                "workflow": workflow,
                "step": self.step,
                "validator": validator,
                "assertions": assertions,
                "assertion_groups": grouped_assertions,
                "uses_signal_stages": uses_signal_stages,
                "ruleset": ruleset,
                "can_manage_assertions": self.user_can_manage_workflow()
                and allow_assertions,
                "supports_assertions": allow_assertions,
                "catalog_entries": catalog_entries,
                "catalog_display": catalog_display,
                "catalog_tab_prefix": f"workflow-step-{self.step.pk}-catalog",
                "validator_default_assertions_count": default_assertions_count,
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
        resequence_workflow_steps(workflow)
        message = _("Workflow step removed.")
        return hx_trigger_response(message, close_modal=None)


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
            return hx_trigger_response(
                status_code=400,
                message=_("Step not found."),
                level="warning",
            )
        if direction == "up" and index > 0:
            steps[index - 1], steps[index] = steps[index], steps[index - 1]
        elif direction == "down" and index < len(steps) - 1:
            steps[index], steps[index + 1] = steps[index + 1], steps[index]
        else:
            return hx_trigger_response(status_code=204)
        with transaction.atomic():
            for pos, item in enumerate(steps, start=1):
                WorkflowStep.objects.filter(pk=item.pk).update(order=1000 + pos)
            resequence_workflow_steps(workflow)
        message = _("Workflow step order updated.")
        return hx_trigger_response(message, close_modal=None)


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
                    getattr(
                        self.step.validator,
                        "allow_custom_assertion_targets",
                        False,
                    ),
                ),
            },
        )
        return context

    def _determine_run_stage_from_form(self, form: RulesetAssertionForm) -> str:
        entry = form.cleaned_data.get("target_catalog_entry")
        if entry and getattr(entry, "run_stage", None):
            return entry.run_stage
        return CatalogRunStage.OUTPUT

    def _stage_filter(self, stage: str) -> Q:
        if stage == CatalogRunStage.INPUT:
            return Q(target_catalog_entry__run_stage=CatalogRunStage.INPUT)
        return Q(
            Q(target_catalog_entry__run_stage=CatalogRunStage.OUTPUT)
            | Q(target_catalog_entry__isnull=True),
        )


class WorkflowStepAssertionCreateView(WorkflowStepAssertionModalBase):
    modal_title = _("Add Assertion")
    submit_label = _("Add Assertion")

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        ruleset = self.get_ruleset()
        stage = self._determine_run_stage_from_form(form)
        stage_q = self._stage_filter(stage)
        max_order = (
            ruleset.assertions.filter(stage_q).aggregate(max_order=models.Max("order"))[
                "max_order"
            ]
            or 0
        )
        assertion = RulesetAssertion.objects.create(
            ruleset=ruleset,
            order=max_order + 10,
            assertion_type=form.cleaned_data["assertion_type"],
            operator=form.cleaned_data["resolved_operator"],
            target_catalog_entry=form.cleaned_data.get("target_catalog_entry"),
            target_field=form.cleaned_data.get("target_field_value") or "",
            severity=form.cleaned_data["severity"],
            when_expression=form.cleaned_data.get("when_expression") or "",
            rhs=form.cleaned_data["rhs_payload"],
            options=form.cleaned_data["options_payload"],
            message_template=form.cleaned_data.get("message_template") or "",
            cel_cache=form.cleaned_data.get("cel_cache") or "",
        )
        messages.success(self.request, _("Assertion added."))
        return hx_trigger_response(
            message=_("Assertion added."),
            close_modal="workflowAssertionModal",
            extra_payload={
                "assertions-changed": {
                    "focus_assertion_id": assertion.pk,
                },
            },
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
            target_catalog_entry=form.cleaned_data.get("target_catalog_entry"),
            target_field=form.cleaned_data.get("target_field_value") or "",
            severity=form.cleaned_data["severity"],
            when_expression=form.cleaned_data.get("when_expression") or "",
            rhs=form.cleaned_data["rhs_payload"],
            options=form.cleaned_data["options_payload"],
            message_template=form.cleaned_data.get("message_template") or "",
            cel_cache=form.cleaned_data.get("cel_cache") or "",
        )
        messages.success(self.request, _("Assertion updated."))
        return hx_trigger_response(
            message=_("Assertion updated."),
            close_modal="workflowAssertionModal",
            extra_payload={
                "assertions-changed": {
                    "focus_assertion_id": assertion.pk,
                },
            },
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
            return hx_trigger_response(
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
            ),
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
        validator = getattr(self.step, "validator", None)
        use_stage_buckets = bool(validator and validator.has_processor)

        if use_stage_buckets:
            grouped = {"input": [], "output": []}
            for item in assertions:
                key = (
                    "input"
                    if item.resolved_run_stage == CatalogRunStage.INPUT
                    else "output"
                )
                grouped[key].append(item)
            target_key = (
                "input"
                if assertion.resolved_run_stage == CatalogRunStage.INPUT
                else "output"
            )
            target_list = grouped[target_key]
            try:
                index = target_list.index(assertion)
            except ValueError:
                return hx_trigger_response(
                    status_code=400,
                    message=_("Assertion not found."),
                )
            if direction == "up" and index > 0:
                target_list[index - 1], target_list[index] = (
                    target_list[index],
                    target_list[index - 1],
                )
            elif direction == "down" and index < len(target_list) - 1:
                target_list[index], target_list[index + 1] = (
                    target_list[index + 1],
                    target_list[index],
                )
            else:
                return hx_trigger_response(status_code=204)
            assertions = grouped["input"] + grouped["output"]
        else:
            try:
                index = assertions.index(assertion)
            except ValueError:
                return hx_trigger_response(
                    status_code=400,
                    message=_("Assertion not found."),
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
                return hx_trigger_response(status_code=204)
        with transaction.atomic():
            for pos, item in enumerate(assertions, start=1):
                RulesetAssertion.objects.filter(pk=item.pk).update(order=pos * 10)
        return hx_redirect_response(
            reverse_with_org(
                "workflows:workflow_step_edit",
                request=request,
                kwargs={
                    "pk": self.get_workflow().pk,
                    "step_id": self.step.pk,
                },
            )
            + "#workflow-step-assertions",
        )
