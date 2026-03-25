"""Core CRUD views for workflows.

Includes listing, detail, JSON export, create, update, delete, archive,
and activation toggle views. Also defines the MAX_STEP_COUNT constant
used across multiple workflow view modules.
"""

import json
import logging
from http import HTTPStatus

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError
from django.db import DatabaseError
from django.db import connection
from django.db import models
from django.db.models import Count
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.template.loader import render_to_string
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import DetailView
from django.views.generic import ListView
from django.views.generic import TemplateView
from django.views.generic.edit import CreateView
from django.views.generic.edit import DeleteView
from django.views.generic.edit import FormView
from django.views.generic.edit import UpdateView

from validibot.core.utils import reverse_with_org
from validibot.projects.models import Project
from validibot.users.constants import RoleCode
from validibot.users.permissions import PermissionCode
from validibot.workflows.constants import WORKFLOW_LIST_LAYOUT_SESSION_KEY
from validibot.workflows.constants import WORKFLOW_LIST_SHOW_ARCHIVED_SESSION_KEY
from validibot.workflows.constants import WorkflowListLayout
from validibot.workflows.form_builder import schema_to_requirement_rows
from validibot.workflows.forms import WorkflowBreakGlassDeleteForm
from validibot.workflows.mixins import WorkflowAccessMixin
from validibot.workflows.mixins import WorkflowFormViewMixin
from validibot.workflows.mixins import WorkflowObjectMixin
from validibot.workflows.models import Workflow
from validibot.workflows.schema_builder import workflow_has_input_form
from validibot.workflows.serializers import WorkflowFullSerializer
from validibot.workflows.views_helpers import public_info_card_context

logger = logging.getLogger(__name__)

MAX_STEP_COUNT = 50
BREAK_GLASS_DELETE_MESSAGE = _(
    "This workflow has issued credentials. Archive it instead, or use the "
    "break-glass delete flow if an owner must remove it from normal product "
    "surfaces."
)


def _issued_credential_model():
    """Return the Pro credential model when available."""

    try:
        from validibot_pro.credentials.models import IssuedCredential
    except Exception:
        return None
    return IssuedCredential


def _workflow_has_issued_credentials(workflow: Workflow) -> bool:
    """Return True when a workflow has any durable issued credentials."""

    issued_credential_model = _issued_credential_model()
    if issued_credential_model is None:
        return False
    table_name = issued_credential_model._meta.db_table
    if table_name not in connection.introspection.table_names():
        return False
    try:
        return issued_credential_model.objects.filter(
            workflow_run__workflow=workflow,
        ).exists()
    except DatabaseError:
        return False


def _workflow_issued_credential_count(workflow: Workflow) -> int:
    """Count durable issued credentials that depend on the workflow."""

    issued_credential_model = _issued_credential_model()
    if issued_credential_model is None:
        return 0
    table_name = issued_credential_model._meta.db_table
    if table_name not in connection.introspection.table_names():
        return 0
    try:
        return issued_credential_model.objects.filter(
            workflow_run__workflow=workflow,
        ).count()
    except DatabaseError:
        return 0


def _compute_workflow_definition_hash(workflow: Workflow) -> str:
    """Compute the locked workflow-definition digest when Pro is installed."""

    try:
        from validibot_pro.credentials.workflow_digest import (
            compute_workflow_definition_hash,
        )
    except Exception:
        return ""
    return compute_workflow_definition_hash(workflow)


def _can_break_glass_delete_workflow(
    workflow: Workflow,
    *,
    user,
    membership,
) -> bool:
    """Restrict break-glass delete to superusers and org owners."""

    if not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if membership is None or not getattr(membership, "is_active", False):
        return False
    if membership.org_id != workflow.org_id:
        return False
    return membership.has_role(RoleCode.OWNER)


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
            .annotate(
                run_count=Count("validation_runs", distinct=True),
                # Count active guest access grants for this workflow
                guest_count=Count(
                    "access_grants",
                    filter=models.Q(access_grants__is_active=True),
                    distinct=True,
                ),
            )
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
    include_tombstoned_workflows = True

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
        membership = getattr(
            self.request.user,
            "membership_for_current_org",
            lambda: None,
        )()
        has_issued_credentials = _workflow_has_issued_credentials(workflow)
        recent_runs = workflow.validation_runs.all().order_by("-created")[:5]
        can_manage_workflow = (
            self.user_can_manage_workflow() and not workflow.is_tombstoned
        )
        can_manage_public_info = can_manage_workflow
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
                "can_manage_activation": can_manage_workflow,
                "show_private_notes": self.user_can_manage_workflow(),
                "public_info_url": public_info_context["public_info_url"],
                "can_manage_public_info": can_manage_public_info,
                "can_launch_workflow": workflow.can_execute(user=self.request.user),
                "can_manage_workflow": can_manage_workflow,
                "can_view_workflow": self.user_can_view_workflow(),
                "workflow_has_runs": workflow.validation_runs.exists(),
                "workflow_has_issued_credentials": has_issued_credentials,
                "issued_credential_count": _workflow_issued_credential_count(
                    workflow,
                ),
                "can_break_glass_delete_workflow": (
                    has_issued_credentials
                    and not workflow.is_tombstoned
                    and _can_break_glass_delete_workflow(
                        workflow,
                        user=self.request.user,
                        membership=membership,
                    )
                ),
            },
        )
        return context


class WorkflowJsonView(WorkflowObjectMixin, TemplateView):
    """
    Read-only JSON representation of a workflow, including all steps and assertions.

    Renders the WorkflowFullSerializer output as pretty-printed JSON in a simple
    page. Useful for debugging, MCP tooling, and API consumers who want to inspect
    the full workflow structure before building integrations.
    """

    template_name = "workflows/workflow_json.html"

    def get_object(self) -> Workflow:
        return (
            Workflow.objects.filter(pk=self.kwargs["pk"])
            .prefetch_related(
                "steps__validator",
                "steps__ruleset__assertions__target_signal_definition",
            )
            .get()
        )

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
        breadcrumbs.append({"name": _("JSON"), "url": ""})
        return breadcrumbs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = self.get_object()
        serializer = WorkflowFullSerializer(workflow, context={"request": self.request})
        context["workflow"] = workflow
        context["json_data"] = json.dumps(serializer.data, indent=2, ensure_ascii=False)
        return context


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
        try:
            response = super().form_valid(form)
        except ValidationError as exc:
            # Workflow.save() calls full_clean() which can raise a model-level
            # ValidationError (e.g., unique constraint on org+slug+version).
            # The form has already passed is_valid() at this point because
            # the slug is auto-generated in save() — the form never sees it.
            # Convert the model error to a form error so the user gets a
            # friendly message instead of a 500.
            form.add_error(None, exc)
            return self.form_invalid(form)
        messages.success(self.request, _("Workflow created."))
        return response

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

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = self.object
        if workflow_has_input_form(workflow):
            context["schema_requirement_rows"] = schema_to_requirement_rows(
                workflow.input_schema,
            )
        return context

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
        try:
            response = super().form_valid(form)
        except ValidationError as exc:
            form.add_error(None, exc)
            return self.form_invalid(form)
        messages.success(self.request, _("Workflow updated."))
        return response

    def get_success_url(self):
        return reverse_with_org(
            "workflows:workflow_detail",
            request=self.request,
            kwargs={"pk": self.object.pk},
        )


class WorkflowDeleteView(WorkflowAccessMixin, DeleteView):
    template_name = "workflows/partials/workflow_confirm_delete.html"

    def _has_issued_credentials(self, workflow: Workflow) -> bool:
        """Return True when the workflow has any durable issued credentials."""
        return _workflow_has_issued_credentials(workflow)

    def _block_delete_response(self, request, workflow: Workflow):
        """Return a response when credential-bearing workflows cannot be deleted."""

        messages.error(request, BREAK_GLASS_DELETE_MESSAGE)
        detail_url = reverse_with_org(
            "workflows:workflow_detail",
            request=request,
            kwargs={"pk": workflow.pk},
        )
        if request.headers.get("HX-Request"):
            response = HttpResponse(status=HTTPStatus.CONFLICT)
            response["HX-Redirect"] = detail_url
            return response
        if request.method == "DELETE":
            return HttpResponse(status=HTTPStatus.CONFLICT)
        return HttpResponseRedirect(detail_url)

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
        if self._has_issued_credentials(self.object):
            return self._block_delete_response(request, self.object)
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


class WorkflowBreakGlassDeleteView(WorkflowObjectMixin, FormView):
    """Tombstone a credential-bearing workflow after explicit owner confirmation.

    Break-glass delete is an exceptional lifecycle action. It removes the
    workflow from normal product surfaces while preserving the underlying row so
    historical runs and signed credentials retain a stable reference.
    """

    template_name = "workflows/workflow_break_glass_delete.html"
    form_class = WorkflowBreakGlassDeleteForm

    def dispatch(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        membership = getattr(
            request.user,
            "membership_for_current_org",
            lambda: None,
        )()
        if not _can_break_glass_delete_workflow(
            workflow,
            user=request.user,
            membership=membership,
        ):
            raise PermissionDenied
        if workflow.is_tombstoned:
            messages.info(
                request,
                _("This workflow has already been tombstoned."),
            )
            return HttpResponseRedirect(self.get_success_url())
        if not _workflow_has_issued_credentials(workflow):
            messages.error(
                request,
                _(
                    "Break-glass delete is only available for workflows with "
                    "issued credentials."
                ),
            )
            return HttpResponseRedirect(self.get_success_url())
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["workflow"] = self.get_workflow()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = self.get_workflow()
        context.update(
            {
                "workflow": workflow,
                "validation_run_count": workflow.validation_runs.count(),
                "issued_credential_count": _workflow_issued_credential_count(
                    workflow,
                ),
                "impact_summary": _(
                    "Existing signed credentials remain cryptographically "
                    "valid, but this workflow will disappear from normal "
                    "listings, launch flows, and editing screens."
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
        breadcrumbs.append({"name": _("Break-glass delete"), "url": ""})
        return breadcrumbs

    def get_success_url(self):
        return reverse_with_org(
            "workflows:workflow_detail",
            request=self.request,
            kwargs={"pk": self.get_workflow().pk},
        )

    def form_valid(self, form):
        workflow = self.get_workflow()
        workflow.tombstone(
            deleted_by=self.request.user,
            reason=form.cleaned_data["deletion_reason"],
            workflow_definition_hash=_compute_workflow_definition_hash(workflow),
        )
        messages.warning(
            self.request,
            _(
                "Workflow tombstoned. Historical runs and credentials remain "
                "available, but the workflow has been removed from normal "
                "authoring and launch surfaces."
            ),
        )
        return HttpResponseRedirect(self.get_success_url())


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

            # Cancel any pending workflow invites since the workflow is no longer
            # accessible.
            self._cancel_pending_invites(workflow)

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

    def _cancel_pending_invites(self, workflow: Workflow) -> int:
        """
        Cancel pending WorkflowInvites for an archived workflow.

        When a workflow is archived, pending invites become pointless since
        the workflow is no longer accessible. This cleans up those invites
        and returns the count of canceled invites.
        """
        from validibot.workflows.models import WorkflowInvite

        return WorkflowInvite.objects.filter(
            workflow=workflow,
            status=WorkflowInvite.Status.PENDING,
        ).update(status=WorkflowInvite.Status.CANCELED)

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
            workflow.is_active
            and not workflow.is_archived
            and not workflow.is_tombstoned
            and can_execute
        )
        workflow.curr_user_can_delete = self._can_manage_workflow_actions(
            workflow,
            self.request.user,
            membership,
        )
        workflow.curr_user_can_edit = workflow.curr_user_can_delete
        workflow.curr_user_can_view = can_view


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
