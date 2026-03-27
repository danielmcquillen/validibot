"""Views for launching workflows and viewing run status.

Includes the workflow launch form, status polling, cancellation,
run detail display, the superuser-only last-run shortcut, and the
preflight input-schema validation endpoint.
"""

from __future__ import annotations

import json
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
from pydantic import ValidationError as PydanticValidationError

from validibot.core.utils import reverse_with_org
from validibot.submissions.constants import SubmissionFileType
from validibot.users.mixins import SuperuserRequiredMixin
from validibot.validations.constants import ValidationRunStatus
from validibot.validations.services.validation_run import ValidationRunService
from validibot.workflows.form_builder import schema_to_django_form
from validibot.workflows.form_builder import schema_to_requirement_rows
from validibot.workflows.mixins import WorkflowLaunchContextMixin
from validibot.workflows.schema_builder import build_pydantic_model
from validibot.workflows.schema_builder import workflow_has_input_form
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

        # Input-schema form and requirements
        input_form = None
        schema_requirement_rows = None
        has_input_form = workflow_has_input_form(workflow)

        if has_input_form:
            schema = workflow.input_schema
            form_class = schema_to_django_form(schema)
            input_form = kwargs.get("input_form") or form_class()
            schema_requirement_rows = schema_to_requirement_rows(schema)

        context.update(
            {
                "workflow": workflow,
                "recent_runs": self.get_recent_runs(workflow),
                "can_execute": can_execute,
                "has_steps": has_steps,
                "launch_form": form,
                "panel_mode": "form",
                "input_form": input_form,
                "has_input_form": has_input_form,
                "schema_requirement_rows": schema_requirement_rows,
            },
        )
        return context

    def post(self, request, *args, **kwargs):
        """Handle submission of the workflow launch form.

        When ``input_mode`` is ``'form'``, the structured input form is
        validated first, then serialized to JSON and injected into a copy
        of ``request.POST`` as the ``payload`` field so the existing
        ``WorkflowLaunchForm`` pipeline sees a normal paste submission.
        """
        start_time = time.perf_counter()
        workflow = self.get_workflow()
        input_mode = request.POST.get("input_mode", "")

        # ── Form-mode branch ────────────────────────────────────────
        if input_mode == "form" and workflow_has_input_form(workflow):
            schema = workflow.input_schema
            form_class = schema_to_django_form(schema)
            input_form = form_class(data=request.POST)

            if not input_form.is_valid():
                self._remember_launch_input_mode(
                    request,
                    None,
                    mode="form",
                )
                context = self.get_context_data(
                    launch_form=self.get_launch_form(workflow=workflow),
                    input_form=input_form,
                )
                return self.render_to_response(context, status=HTTPStatus.OK)

            # Serialize form data to JSON, omitting unset optional fields
            form_data = {
                k: v
                for k, v in input_form.cleaned_data.items()
                if v is not None and v != ""
            }

            # Run Pydantic validation
            pydantic_model = build_pydantic_model(schema)
            try:
                pydantic_model(**form_data)
            except PydanticValidationError as exc:
                errors = [
                    f"{'.'.join(str(part) for part in e['loc'])}: {e['msg']}"
                    for e in exc.errors()
                ]
                launch_form = self.get_launch_form(workflow=workflow)
                for error_msg in errors:
                    launch_form.add_error(None, error_msg)
                self._remember_launch_input_mode(request, None, mode="form")
                context = self.get_context_data(
                    launch_form=launch_form,
                    input_form=input_form,
                )
                return self.render_to_response(context, status=HTTPStatus.OK)

            # Inject JSON payload into a mutable copy of POST data
            mutable_post = request.POST.copy()
            mutable_post["payload"] = json.dumps(form_data)
            mutable_post["file_type"] = SubmissionFileType.JSON
            mutable_post["input_mode"] = "paste"  # downstream sees paste mode

            form = self.get_launch_form(
                workflow=workflow,
                data=mutable_post,
                files=request.FILES,
            )
        else:
            # ── Existing upload/paste path ───────────────────────────
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

        # ── Authoritative schema validation for all JSON submissions ──
        # The form-mode branch above already validates via Pydantic before
        # reaching here.  For paste/upload of JSON, we enforce the same
        # canonical contract so users cannot bypass it by switching modes.
        if (
            input_mode != "form"
            and workflow_has_input_form(workflow)
            and form.cleaned_data.get("file_type") == SubmissionFileType.JSON
        ):
            # Determine the JSON text to validate — either pasted text
            # or the content of an uploaded file.
            payload = form.cleaned_data.get("payload", "")
            attachment = form.cleaned_data.get("attachment")
            if not payload and attachment:
                try:
                    payload = attachment.read().decode("utf-8")
                    attachment.seek(0)
                except (UnicodeDecodeError, AttributeError):
                    form.add_error(
                        None,
                        _("Uploaded file is not valid UTF-8 text."),
                    )
                    context = self.get_context_data(launch_form=form)
                    return self.render_to_response(
                        context,
                        status=HTTPStatus.OK,
                    )

            if payload:
                schema_errors = self._validate_json_against_schema(
                    payload,
                    workflow.input_schema,
                )
                if schema_errors:
                    for err in schema_errors:
                        form.add_error(None, err)
                    context = self.get_context_data(launch_form=form)
                    return self.render_to_response(
                        context,
                        status=HTTPStatus.OK,
                    )

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

    @staticmethod
    def _validate_json_against_schema(
        payload: str,
        schema: dict,
    ) -> list[str]:
        """Validate a JSON payload string against the workflow's input schema.

        Returns a list of human-readable error strings (empty on success).
        This is the authoritative contract gate for paste/upload paths,
        so schema-driven workflows cannot be bypassed by switching input
        mode.  Malformed JSON is rejected here rather than silently passed
        through, because ``WorkflowLaunchForm.clean()`` does not validate
        JSON syntax.
        """
        errors: list[str] = []
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            errors.append(
                str(
                    _("Invalid JSON: %(error)s (line %(line)s, column %(col)s)")
                    % {"error": exc.msg, "line": exc.lineno, "col": exc.colno}
                ),
            )
            return errors
        except TypeError:
            errors.append(str(_("Invalid input: expected a JSON string.")))
            return errors

        if not isinstance(data, dict):
            errors.append(
                str(
                    _("Input must be a JSON object, not %(type)s.")
                    % {"type": type(data).__name__}
                ),
            )
            return errors

        pydantic_model = build_pydantic_model(schema)
        try:
            pydantic_model(**data)
        except PydanticValidationError as exc:
            for e in exc.errors():
                loc = ".".join(str(part) for part in e["loc"])
                errors.append(f"{loc}: {e['msg']}")
        return errors


class WorkflowLaunchStatusView(WorkflowLaunchContextMixin, View):
    http_method_names = ["get"]

    def get(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        run_id = kwargs.get("run_id")
        run = self.load_run_for_display(workflow=workflow, run_id=run_id)
        if run is None:
            raise Http404
        context = self.build_run_detail_context(
            workflow=workflow,
            run=run,
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


class WorkflowLaunchValidateInputView(WorkflowLaunchContextMixin, View):
    """No-side-effect preflight validation of launch-page input.

    Validates the current input against the workflow's ``input_schema``
    without creating a ``Submission`` or ``ValidationRun``.  Returns a
    partial HTML fragment for the validation status panel.
    """

    http_method_names = ["post"]

    def post(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        if not workflow_has_input_form(workflow):
            raise Http404

        schema = workflow.input_schema
        pydantic_model = build_pydantic_model(schema)

        input_mode = request.POST.get("input_mode", "form")
        validation_errors: list[str] = []

        if input_mode == "form":
            form_class = schema_to_django_form(schema)
            input_form = form_class(data=request.POST)
            if not input_form.is_valid():
                for field_name, error_list in input_form.errors.items():
                    label = field_name if field_name != "__all__" else ""
                    for err in error_list:
                        prefix = f"{label}: " if label else ""
                        validation_errors.append(f"{prefix}{err}")
            else:
                form_data = {
                    k: v
                    for k, v in input_form.cleaned_data.items()
                    if v is not None and v != ""
                }
                try:
                    pydantic_model(**form_data)
                except PydanticValidationError as exc:
                    for e in exc.errors():
                        loc = ".".join(str(part) for part in e["loc"])
                        validation_errors.append(f"{loc}: {e['msg']}")
        else:
            # Paste-mode: parse JSON and validate
            raw_payload = request.POST.get("payload", "").strip()
            if not raw_payload:
                validation_errors.append(str(_("No input provided.")))
            else:
                try:
                    data = json.loads(raw_payload)
                except json.JSONDecodeError as exc:
                    validation_errors.append(
                        str(
                            _("Invalid JSON: %(error)s (line %(line)s, column %(col)s)")
                            % {"error": exc.msg, "line": exc.lineno, "col": exc.colno}
                        )
                    )
                    data = None

                if data is not None:
                    if not isinstance(data, dict):
                        validation_errors.append(
                            str(_("Input must be a JSON object, not %(type)s."))
                            % {"type": type(data).__name__}
                        )
                    else:
                        try:
                            pydantic_model(**data)
                        except PydanticValidationError as exc:
                            for e in exc.errors():
                                loc = ".".join(str(part) for part in e["loc"])
                                validation_errors.append(
                                    f"{loc}: {e['msg']}",
                                )

        context = {
            "validation_success": not validation_errors,
            "validation_errors": validation_errors,
        }
        return render(
            request,
            "workflows/launch/partials/schema_validation_status.html",
            context=context,
        )
