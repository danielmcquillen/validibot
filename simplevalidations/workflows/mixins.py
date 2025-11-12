import json
import logging
import uuid
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.utils.functional import Promise
from django.utils.translation import gettext_lazy as _

from simplevalidations.core.mixins import BreadcrumbMixin
from simplevalidations.core.utils import reverse_with_org
from simplevalidations.projects.models import Project
from simplevalidations.users.constants import RoleCode
from simplevalidations.users.models import User
from simplevalidations.validations.constants import (
    ADVANCED_VALIDATION_TYPES,
    ValidationRunStatus,
)
from simplevalidations.validations.models import Ruleset, ValidationRun
from simplevalidations.workflows.constants import WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY
from simplevalidations.workflows.forms import WorkflowForm, WorkflowLaunchForm
from simplevalidations.workflows.models import Workflow, WorkflowStep
from simplevalidations.workflows.views_helpers import ensure_advanced_ruleset

logger = logging.getLogger(__name__)


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
            ruleset = ensure_advanced_ruleset(
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

    launch_panel_template_name = "workflows/launch/partials/launch_panel.html"

    run_status_template_name = "workflows/launch/partials/run_status.html"

    status_area_template_name = "workflows/launch/partials/status_area.html"

    polling_statuses = {
        ValidationRunStatus.PENDING,
        ValidationRunStatus.RUNNING,
    }

    def get_poll_interval_seconds(self) -> int:
        return int(getattr(settings, "WORKFLOW_RUN_POLL_INTERVAL_SECONDS", 3))

    def _collect_run_display_data(
        self,
        run: ValidationRun | None,
    ) -> tuple[list[Any], list[Any], bool]:
        if not run:
            return [], [], False
        step_runs = list(run.step_runs.order_by("step_order"))
        findings = list(run.findings.order_by("severity", "-created")[:10])
        run_in_progress = run.status in self.polling_statuses
        return step_runs, findings, run_in_progress

    def build_status_area_context(
        self,
        *,
        workflow: Workflow,
        active_run: ValidationRun | None,
    ) -> dict[str, object]:
        step_runs, findings, run_in_progress = self._collect_run_display_data(
            active_run,
        )
        poll_interval = self.get_poll_interval_seconds()
        status_url = None
        detail_url = None
        cancel_url = None
        if active_run:
            status_url = reverse_with_org(
                "workflows:workflow_launch_status",
                request=self.request,
                kwargs={"pk": workflow.pk, "run_id": active_run.pk},
            )
            detail_url = reverse_with_org(
                "validations:validation_detail",
                request=self.request,
                kwargs={"pk": active_run.pk},
            )
            if run_in_progress:
                cancel_url = reverse_with_org(
                    "workflows:workflow_launch_cancel",
                    request=self.request,
                    kwargs={"pk": workflow.pk, "run_id": active_run.pk},
                )
        launch_url = reverse_with_org(
            "workflows:workflow_launch",
            request=self.request,
            kwargs={"pk": workflow.pk},
        )
        previous_runs_url = reverse_with_org(
            "workflows:workflow_validation_list",
            request=self.request,
            kwargs={"pk": workflow.pk},
        )
        return {
            "active_run": active_run,
            "active_run_step_runs": step_runs,
            "active_run_findings": findings,
            "run_in_progress": run_in_progress,
            "polling_statuses": self.polling_statuses,
            "poll_interval_seconds": poll_interval,
            "status_url": status_url,
            "detail_url": detail_url,
            "cancel_url": cancel_url,
            "launch_url": launch_url,
            "previous_runs_url": previous_runs_url,
        }

    def get_recent_runs(self, workflow: Workflow, limit: int = 5):
        return list(
            ValidationRun.objects.filter(workflow=workflow)
            .select_related("submission", "user")
            .order_by("-created")[:limit],
        )

    def _remember_launch_input_mode(self, request, payload: str | None) -> None:
        mode = "paste" if (payload or "").strip() else "upload"
        try:
            request.session[WORKFLOW_LAUNCH_INPUT_MODE_SESSION_KEY] = mode
            request.session.modified = True
        except Exception:  # pragma: no cover - defensive
            logger.exception("Unable to persist workflow launch input mode preference.")

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
        status_context = self.build_status_area_context(
            workflow=workflow,
            active_run=active_run,
        )
        context = {
            "workflow": workflow,
            "launch_form": form,
            "can_execute": workflow.can_execute(user=self.request.user),
            "has_steps": has_steps,
            "recent_runs": self.get_recent_runs(workflow),
        }
        context.update(status_context)
        return context

    def _launch_response(
        self,
        request,
        *,
        workflow: Workflow,
        form: WorkflowLaunchForm | None,
        active_run: ValidationRun | None,
        status_code: int,
        toast: dict[str, str] | None = None,
        fragment: str = "panel",
    ):
        is_htmx = request.headers.get("HX-Request") == "true"
        current_fragment = fragment
        if fragment == "status" and not is_htmx:
            current_fragment = "panel"

        if current_fragment == "status":
            context = {"workflow": workflow}
            context.update(
                self.build_status_area_context(
                    workflow=workflow,
                    active_run=active_run,
                ),
            )
            template_name = self.status_area_template_name
        else:
            form = form or self.get_launch_form(workflow=workflow)
            context = self.build_launch_context(
                workflow=workflow,
                form=form,
                active_run=active_run,
            )
            template_name = (
                self.launch_panel_template_name
                if is_htmx
                else "workflows/launch/workflow_launch.html"
            )

        response = render(
            request,
            template_name,
            context=context,
            status=status_code,
        )

        if is_htmx and current_fragment == "panel":
            response["HX-Retarget"] = "#workflow-launch-panel"

        if toast:
            sanitized_toast = {
                key: str(value) if isinstance(value, Promise) else value
                for key, value in toast.items()
            }
            response["HX-Trigger"] = json.dumps({"toast": sanitized_toast})
        return response


class WorkflowFormViewMixin(WorkflowAccessMixin):
    form_class = WorkflowForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.request.user
        return kwargs

    def _default_project_for_org(self) -> Project | None:
        user = getattr(self.request, "user", None)
        org = getattr(user, "get_current_org", lambda: None)() if user else None
        if not org:
            return None
        project = Project.objects.filter(org=org, is_default=True).first()
        if project:
            return project
        return Project.objects.filter(org=org).order_by("name").first()
