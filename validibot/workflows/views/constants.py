"""Views for workflow-level Constants configuration (the ``c.*`` namespace).

Constants (ADR-2026-06-18) are fixed, author-defined values referenced in
assertions as ``c.<name>``. Unlike signals they have no source path and never
"resolve" — they come from the workflow definition. The editor therefore
mirrors the signal-mapping editor's two-template modal CRUD pattern but is
simpler: there is no sample-data card and no bulk-add-from-payload flow.

- **Page view** (``WorkflowConstantView``): the full editor page with the
  constants table and modal shell; returns just the table partial on an HTMx
  ``constants-changed`` refresh.
- **Create/Edit modals**: GET returns the form partial; POST validates/saves
  and returns an ``hx_trigger_response`` (204 + ``HX-Trigger`` on success, or a
  200 with the re-rendered form on validation error).
- **Delete**: blocked if the constant is referenced by any assertion (you'd
  silently break the rule); the model's own guard separately blocks deletion
  once the workflow has runs.
- **Move**: swaps ``position`` with the adjacent constant.
"""

from __future__ import annotations

import logging
import re
from http import HTTPStatus
from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic.edit import FormView

from validibot.core.view_helpers import hx_trigger_response
from validibot.workflows.forms import WorkflowConstantForm
from validibot.workflows.mixins import WorkflowObjectMixin
from validibot.workflows.models import WorkflowConstant

logger = logging.getLogger(__name__)


class WorkflowConstantView(WorkflowObjectMixin, View):
    """GET: Render the Constants editor page (or just the table on HTMx).

    Requires **manage** permission on the workflow.
    """

    def get(self, request, pk):
        workflow = self.get_workflow()
        if not self.user_can_manage_workflow():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)

        constants = WorkflowConstant.objects.filter(
            workflow=workflow,
        ).order_by("position")

        context = {
            "workflow": workflow,
            "constants": constants,
            "can_manage_workflow": self.user_can_manage_workflow(),
            "breadcrumbs": [
                {
                    "name": _("Workflows"),
                    "url": reverse("workflows:workflow_list"),
                },
                self.workflow_breadcrumb_item(
                    workflow,
                    url=reverse(
                        "workflows:workflow_detail",
                        kwargs={"pk": workflow.pk},
                    ),
                ),
                {"name": _("Constants"), "url": ""},
            ],
        }

        if request.headers.get("HX-Request"):
            return render(
                request,
                "workflows/partials/constant_table.html",
                context,
            )

        return render(request, "workflows/workflow_constants.html", context)


# ── Modal CRUD views ─────────────────────────────────────────────────


class WorkflowConstantCreateView(WorkflowObjectMixin, FormView):
    """Create a new constant via modal form."""

    template_name = "workflows/partials/constant_form.html"
    form_class = WorkflowConstantForm

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["workflow"] = self.get_workflow()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "modal_title": _("Add Constant"),
                "form_action": self.request.path,
                "submit_label": _("Add Constant"),
                "workflow": self.get_workflow(),
            },
        )
        return context

    def render_to_response(self, context, **response_kwargs):
        return render(
            self.request,
            self.template_name,
            context,
            status=response_kwargs.get("status", 200),
        )

    def form_valid(self, form):
        constant = form.save_constant(self.get_workflow())
        messages.success(self.request, _("Constant added."))
        return hx_trigger_response(
            message=_("Constant added."),
            close_modal="constantModal",
            extra_payload={"constants-changed": {"focus_constant_id": constant.pk}},
            include_steps_changed=False,
        )


class WorkflowConstantEditView(WorkflowObjectMixin, FormView):
    """Edit an existing constant via modal form."""

    template_name = "workflows/partials/constant_form.html"
    form_class = WorkflowConstantForm

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def _get_constant(self) -> WorkflowConstant:
        if not hasattr(self, "_constant"):
            self._constant = get_object_or_404(
                WorkflowConstant,
                pk=self.kwargs.get("constant_id"),
                workflow=self.get_workflow(),
            )
        return self._constant

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["workflow"] = self.get_workflow()
        kwargs["exclude_constant_id"] = self._get_constant().pk
        return kwargs

    def get_initial(self):
        constant = self._get_constant()
        return {
            "name": constant.name,
            "data_type": constant.data_type,
            "value": _value_for_form(constant),
            "description": constant.description,
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "modal_title": _("Edit Constant"),
                "form_action": self.request.path,
                "submit_label": _("Save Changes"),
                "workflow": self.get_workflow(),
            },
        )
        return context

    def render_to_response(self, context, **response_kwargs):
        return render(
            self.request,
            self.template_name,
            context,
            status=response_kwargs.get("status", 200),
        )

    def form_valid(self, form):
        constant = self._get_constant()
        form.save_constant(self.get_workflow(), instance=constant)
        messages.success(self.request, _("Constant updated."))
        return hx_trigger_response(
            message=_("Constant updated."),
            close_modal="constantModal",
            extra_payload={"constants-changed": {"focus_constant_id": constant.pk}},
            include_steps_changed=False,
        )


def _value_for_form(constant: WorkflowConstant) -> str:
    """Render a stored constant value for editing in the value textarea.

    LIST/OBJECT come back as JSON text; scalar types as their plain string so
    the author edits exactly what they typed (``0.40`` stays ``0.40``).
    """
    import json

    from validibot.workflows.constants import WorkflowConstantType

    if constant.data_type in {
        WorkflowConstantType.LIST,
        WorkflowConstantType.OBJECT,
    }:
        return json.dumps(constant.value)
    if constant.data_type == WorkflowConstantType.BOOLEAN:
        return "true" if constant.value else "false"
    return str(constant.value)


def _find_constant_references(workflow, constant_name: str) -> list[str]:
    """Find assertions in this workflow that reference ``c.<name>``.

    Mirrors the signal reference check: searches CEL expressions, the cached
    CEL preview, guard conditions, and a Basic assertion's ``target_data_path``
    for ``c.<name>`` / ``const.<name>`` so a delete that would silently break a
    rule (CEL or Basic) is blocked with a clear message.
    """
    from validibot.validations.models import RulesetAssertion

    pattern = re.compile(rf"\b(?:c|const)\.{re.escape(constant_name)}\b")

    steps = workflow.steps.select_related("ruleset", "validator").all()
    ruleset_to_step: dict[int, Any] = {}
    for step in steps:
        if step.ruleset_id:
            ruleset_to_step[step.ruleset_id] = step
        if step.validator_id and hasattr(step.validator, "default_ruleset"):
            default_rs = step.validator.default_ruleset
            if default_rs:
                ruleset_to_step.setdefault(default_rs.pk, step)

    if not ruleset_to_step:
        return []

    references: list[str] = []
    for assertion in RulesetAssertion.objects.filter(
        ruleset_id__in=ruleset_to_step.keys(),
    ):
        texts = []
        if isinstance(assertion.rhs, dict) and assertion.rhs.get("expr"):
            texts.append(assertion.rhs["expr"])
        if assertion.cel_cache:
            texts.append(assertion.cel_cache)
        if assertion.when_expression:
            texts.append(assertion.when_expression)
        # A Basic assertion can reference a constant as its TARGET
        # (``target_data_path = "c.energy_price"``, ADR-2026-06-18), not only
        # inside a CEL expression. Scan it too, or deleting a constant that a
        # Basic assertion depends on would slip past this guard.
        if assertion.target_data_path:
            texts.append(assertion.target_data_path)
        for text in texts:
            if pattern.search(text):
                step = ruleset_to_step.get(assertion.ruleset_id)
                if step:
                    step_label = f"Step {step.step_number}: {step.name}"
                else:
                    step_label = _("Unknown step")
                references.append(f'{step_label} — "{assertion}"')
                break
    return references


class WorkflowConstantDeleteView(WorkflowObjectMixin, View):
    """Delete a constant, unless it is referenced by an assertion."""

    def post(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        workflow = self.get_workflow()
        constant = get_object_or_404(
            WorkflowConstant,
            pk=self.kwargs.get("constant_id"),
            workflow=workflow,
        )

        references = _find_constant_references(workflow, constant.name)
        if references:
            error_msg = _(
                "Cannot delete constant '%(name)s' — it is referenced in: "
                "%(refs)s. Remove the references first.",
            ) % {"name": constant.name, "refs": "; ".join(references)}
            return hx_trigger_response(
                message=str(error_msg),
                level="error",
                status_code=200,
                close_modal=None,
                extra_payload={"constants-changed": False},
                include_steps_changed=False,
            )

        constant.delete()
        messages.success(request, _("Constant removed."))
        return hx_trigger_response(
            message=_("Constant removed."),
            close_modal=None,
            extra_payload={"constants-changed": True},
            include_steps_changed=False,
        )


class WorkflowConstantMoveView(WorkflowObjectMixin, View):
    """Move a constant up or down in the display order."""

    def post(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        workflow = self.get_workflow()
        constant = get_object_or_404(
            WorkflowConstant,
            pk=self.kwargs.get("constant_id"),
            workflow=workflow,
        )
        direction = request.POST.get("direction")
        constants = list(
            WorkflowConstant.objects.filter(workflow=workflow).order_by(
                "position",
                "pk",
            ),
        )
        index = constants.index(constant)

        if direction == "up" and index > 0:
            constants[index - 1], constants[index] = (
                constants[index],
                constants[index - 1],
            )
        elif direction == "down" and index < len(constants) - 1:
            constants[index], constants[index + 1] = (
                constants[index + 1],
                constants[index],
            )
        else:
            return hx_trigger_response(
                status_code=204,
                close_modal=None,
                include_steps_changed=False,
            )

        with transaction.atomic():
            for pos, item in enumerate(constants, start=1):
                WorkflowConstant.objects.filter(pk=item.pk).update(position=pos * 10)

        return hx_trigger_response(
            close_modal=None,
            extra_payload={"constants-changed": True},
            include_steps_changed=False,
        )
