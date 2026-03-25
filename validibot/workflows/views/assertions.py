"""Workflow step assertion views.

Views for listing workflow validations, and CRUD operations on ruleset
assertions within workflow steps (create, update, delete, move).
"""

import logging

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import models
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import ListView
from django.views.generic.edit import FormView

from validibot.core.utils import reverse_with_org
from validibot.core.view_helpers import hx_redirect_response
from validibot.core.view_helpers import hx_trigger_response
from validibot.validations.constants import CatalogRunStage
from validibot.validations.forms import RulesetAssertionForm
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import ValidationRun
from validibot.workflows.mixins import WorkflowAccessMixin
from validibot.workflows.mixins import WorkflowStepAssertionsMixin

logger = logging.getLogger(__name__)


class WorkflowValidationListView(WorkflowAccessMixin, ListView):
    template_name = "validations/workflow_validation_list.html"
    context_object_name = "validations"
    include_tombstoned_workflows = True

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
        signal = form.cleaned_data.get("resolved_signal")
        if signal and getattr(signal, "direction", None):
            return signal.direction
        return CatalogRunStage.OUTPUT

    def _resolve_signal_definition(self, form: RulesetAssertionForm):
        """Get the resolved SignalDefinition from the form's cleaned data.

        Returns the SignalDefinition object for signal-backed assertions,
        or None for custom targets (which use target_data_path instead).
        """
        return form.cleaned_data.get("resolved_signal")

    def _stage_filter(self, stage: str) -> Q:
        if stage == CatalogRunStage.INPUT:
            return Q(
                target_signal_definition__direction=CatalogRunStage.INPUT,
            )
        return Q(
            Q(target_signal_definition__direction=CatalogRunStage.OUTPUT)
            | Q(target_signal_definition__isnull=True),
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
        signal_def = self._resolve_signal_definition(form)
        assertion = RulesetAssertion.objects.create(
            ruleset=ruleset,
            order=max_order + 10,
            assertion_type=form.cleaned_data["assertion_type"],
            operator=form.cleaned_data["resolved_operator"],
            target_signal_definition=signal_def,
            target_data_path=form.cleaned_data.get("target_data_path_value") or "",
            severity=form.cleaned_data["severity"],
            when_expression=form.cleaned_data.get("when_expression") or "",
            rhs=form.cleaned_data["rhs_payload"],
            options=form.cleaned_data["options_payload"],
            message_template=form.cleaned_data.get("message_template") or "",
            success_message=form.cleaned_data.get("success_message") or "",
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
        signal_def = self._resolve_signal_definition(form)
        RulesetAssertion.objects.filter(pk=assertion.pk).update(
            assertion_type=form.cleaned_data["assertion_type"],
            operator=form.cleaned_data["resolved_operator"],
            target_signal_definition=signal_def,
            target_data_path=form.cleaned_data.get("target_data_path_value") or "",
            severity=form.cleaned_data["severity"],
            when_expression=form.cleaned_data.get("when_expression") or "",
            rhs=form.cleaned_data["rhs_payload"],
            options=form.cleaned_data["options_payload"],
            message_template=form.cleaned_data.get("message_template") or "",
            success_message=form.cleaned_data.get("success_message") or "",
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
            + f"#assertion-card-{assertion.pk}",
        )
