"""Public-facing workflow pages.

Views for the public workflow directory listing, individual workflow info
pages, and management of public info and visibility settings.
"""

import logging

from django.contrib import messages
from django.db import models
from django.http import HttpResponse
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import DetailView
from django.views.generic import ListView
from django.views.generic import UpdateView

from validibot.actions.models import SignedCredentialAction
from validibot.actions.models import SlackMessageAction
from validibot.core.utils import pretty_json
from validibot.core.utils import pretty_xml
from validibot.core.utils import reverse_with_org
from validibot.validations.constants import ValidationType
from validibot.workflows.forms import WorkflowPublicInfoForm
from validibot.workflows.mixins import WorkflowObjectMixin
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep
from validibot.workflows.views_helpers import public_info_card_context

logger = logging.getLogger(__name__)


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
                models.Q(make_info_page_public=True) | models.Q(pk__in=accessible_ids),
            )
        else:
            queryset = queryset.filter(make_info_page_public=True)

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
            Workflow.objects.filter(make_info_page_public=True)
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

        user_has_access = user.is_authenticated and workflow.can_execute(user=user)

        recent_runs = []
        if user_has_access:
            recent_runs = list(
                workflow.validation_runs.select_related("user").order_by(
                    "-created",
                )[:5],
            )
        # Input requirements rows for the public info page
        schema_requirement_rows = None
        from validibot.workflows.form_builder import schema_to_requirement_rows
        from validibot.workflows.schema_builder import workflow_has_input_form

        if workflow_has_input_form(workflow):
            schema_requirement_rows = schema_to_requirement_rows(workflow.input_schema)

        context.update(
            {
                "steps": steps,
                "recent_runs": recent_runs,
                "user_has_access": user_has_access,
                "schema_requirement_rows": schema_requirement_rows,
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
        elif isinstance(variant, SignedCredentialAction):
            summary["credential_template"] = (
                variant.get_credential_template_display_name()
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
        desired_visibility = bool(form.cleaned_data.get("make_info_page_public"))
        if workflow.make_info_page_public != desired_visibility:
            workflow.make_info_page_public = desired_visibility
            workflow.save(update_fields=["make_info_page_public"])
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


class WorkflowPublicVisibilityUpdateView(WorkflowObjectMixin, View):
    """Toggle whether the workflow's public info page is visible."""

    def post(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            return HttpResponse(status=403)
        workflow = self.get_workflow()
        raw_state = (request.POST.get("make_info_page_public") or "").strip().lower()
        if raw_state in {"true", "1", "on"}:
            new_state = True
        elif raw_state in {"false", "0", "off"}:
            new_state = False
        else:
            new_state = not workflow.make_info_page_public

        if workflow.make_info_page_public != new_state:
            workflow.make_info_page_public = new_state
            workflow.save(update_fields=["make_info_page_public"])

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
