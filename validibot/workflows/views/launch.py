"""Views for launching workflows and viewing run status.

Includes the workflow launch form, status polling, cancellation,
run detail display, and the superuser-only last-run shortcut.
"""

import logging
import time
from http import HTTPStatus

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError
from django.http import Http404
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView

from validibot.core.utils import reverse_with_org
from validibot.users.mixins import SuperuserRequiredMixin
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.services.validation_run import ValidationRunService
from validibot.workflows.mixins import WorkflowLaunchContextMixin
from validibot.workflows.views_launch_helpers import LaunchValidationError
from validibot.workflows.views_launch_helpers import build_submission_from_form
from validibot.workflows.views_launch_helpers import launch_web_validation_run

logger = logging.getLogger(__name__)


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
        can_execute = workflow.can_execute(user=self.request.user)
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
