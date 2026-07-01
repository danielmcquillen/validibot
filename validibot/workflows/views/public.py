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

from validibot.actions.models import SlackMessageAction
from validibot.core.utils import pretty_json
from validibot.core.utils import pretty_xml
from validibot.core.utils import reverse_with_org
from validibot.validations.constants import ValidationType
from validibot.workflows.forms import WorkflowPublicInfoForm
from validibot.workflows.mixins import WorkflowObjectMixin
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep
from validibot.workflows.public_tabular import build_tabular_public_details
from validibot.workflows.version_utils import get_latest_workflow_ids
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
        queryset = Workflow.objects.filter(
            is_active=True,
            is_tombstoned=False,
        )
        if user.is_authenticated:
            accessible_ids = (
                Workflow.objects.for_user(user)
                .filter(is_active=True, is_tombstoned=False)
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

        queryset = (
            queryset.select_related("org", "project", "user")
            .prefetch_related("steps")
            .distinct()
        )
        latest_ids = get_latest_workflow_ids(queryset)
        return queryset.filter(pk__in=latest_ids).order_by("name", "pk")

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
        """Limit the info page to workflows the requester may see.

        The visibility contract mirrors :class:`PublicWorkflowListView`:

        * **Anonymous visitors** see a workflow's info page only once its
          author has published it (``make_info_page_public=True``).
        * **Authenticated users** *also* see the info page of any workflow
          they can access via :meth:`WorkflowQuerySet.for_user` — org
          membership granting ``WORKFLOW_VIEW``, the workflow's creator, a
          per-workflow guest grant, or org-wide guest access — even while
          the page is still marked private.

        Without that second branch a teammate with access would hit a 404
        here while the public directory lists the workflow for them and the
        workflow's own "Workflow Public Info Page" status card promises
        "Only teammates with access can currently view the workflow info
        page." The launch control and recent-runs block stay separately
        gated by ``can_execute`` in :meth:`get_context_data`, so a
        view-only teammate sees the page without gaining execute access.

        ``is_tombstoned`` rows are excluded on every path: a deleted
        workflow's info page must 404 for everyone regardless of access.
        """
        queryset = (
            Workflow.objects.filter(is_tombstoned=False)
            .select_related("org", "project", "user")
            .prefetch_related(
                "steps",
                "steps__validator",
                "steps__ruleset",
                "steps__action",
                "steps__action__definition",
            )
        )
        user = self.request.user
        if user.is_authenticated:
            accessible_ids = Workflow.objects.for_user(user).values_list(
                "pk",
                flat=True,
            )
            return queryset.filter(
                models.Q(make_info_page_public=True) | models.Q(pk__in=accessible_ids),
            )
        return queryset.filter(make_info_page_public=True)

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

        # Workflow Constants (c.* namespace, ADR-2026-06-18) — shown on the
        # public info page so a submitter can see the fixed thresholds their
        # data will be judged against BEFORE they submit. A constant is a
        # workflow-defined literal (never submission-derived), so publishing its
        # value here is always safe. Visibility inherits the info page's own
        # rules — this view already gates who can see the page.
        from validibot.workflows.models import WorkflowConstant

        constants = list(
            WorkflowConstant.objects.filter(workflow=workflow).order_by("position"),
        )

        context.update(
            {
                "steps": steps,
                "recent_runs": recent_runs,
                "user_has_access": user_has_access,
                "schema_requirement_rows": schema_requirement_rows,
                "constants": constants,
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
            step.public_tabular = None

            if step.validator is None:
                if step.action:
                    self._populate_public_action(step)
                continue

            vtype = step.validator.validation_type
            # Tabular steps get a submitter-facing "what your CSV must look
            # like" breakdown instead of a raw schema dump — see
            # _populate_public_tabular and the public_tabular_details partial.
            if vtype == ValidationType.TABULAR:
                self._populate_public_tabular(step)
                continue
            if vtype not in {ValidationType.JSON_SCHEMA, ValidationType.XML_SCHEMA}:
                continue

            schema_content: str | None = None
            schema_language: str | None = None
            if step.display_schema:
                schema_content, schema_language = self._load_schema_content(step)
                if not schema_content:
                    logger.warning(
                        "Step %s has display_schema=True but no schema content found "
                        "(ruleset=%s, has_config_preview=%s)",
                        step.pk,
                        step.ruleset_id,
                        bool((step.display_settings or {}).get("schema_text_preview")),
                    )

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

        step.public_action_meta = {
            "category_label": definition.get_action_category_display(),
            "type": definition.type,
            "icon": definition.icon or "bi-gear",
            "definition_name": definition.name,
        }
        step.public_action_summary = summary

    def _populate_public_tabular(self, step: WorkflowStep) -> None:
        """Attach the submitter-facing Tabular detail model to ``step``.

        Reads the step's Table Schema descriptor (``ruleset.rules``) and stored
        dialect (step ``config`` plus ruleset ``metadata``) and builds the
        structure the ``public_tabular_details`` partial renders. Leaves
        ``public_tabular`` as ``None`` when schema display is disabled or the
        step has no usable schema, so private authoring details are not exposed
        and a half-configured step cannot break the public page.
        """
        if not step.display_schema:
            return

        ruleset = step.ruleset
        schema_text = ""
        if ruleset is not None:
            try:
                schema_text = ruleset.rules or ""
            except Exception:
                logger.exception(
                    "Failed to load tabular rules for step",
                    extra={"step_id": step.pk},
                )
        step.public_tabular = build_tabular_public_details(
            schema_text=schema_text,
            # Merge both buckets: the dialect (delimiter/encoding/has_header) is
            # semantic (``config``) while the pre-computed ``delimiter_label`` is
            # cosmetic (``display_settings``). The builder recomputes the label
            # if absent, so this only honours a stored one.
            config={**(step.display_settings or {}), **(step.config or {})},
            metadata=(ruleset.metadata if ruleset else {}) or {},
        )

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
            schema_text = (step.display_settings or {}).get("schema_text_preview", "")

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
            self.workflow_breadcrumb_item(
                workflow,
                url=reverse_with_org(
                    "workflows:workflow_detail",
                    request=self.request,
                    kwargs={"pk": workflow.pk},
                ),
            ),
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
