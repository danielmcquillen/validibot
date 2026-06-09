"""Workflow step assertion views.

Views for listing workflow validations, and CRUD operations on ruleset
assertions within workflow steps (create, update, delete, move).
"""

import logging
import re

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import ListView
from django.views.generic import TemplateView
from django.views.generic.edit import FormView

from validibot.core.utils import reverse_with_org
from validibot.core.view_helpers import hx_trigger_response
from validibot.validations.constants import AssertionType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import ValidationType
from validibot.validations.forms import RulesetAssertionForm
from validibot.validations.models import RulesetAssertion
from validibot.validations.models import ValidationRun
from validibot.workflows.mixins import WorkflowAccessMixin
from validibot.workflows.mixins import WorkflowObjectMixin
from validibot.workflows.mixins import WorkflowStepAssertionsMixin
from validibot.workflows.models import WorkflowStep
from validibot.workflows.services.assertion_mutations import AssertionMutationService
from validibot.workflows.views_helpers import ensure_advanced_ruleset
from validibot.workflows.views_helpers import get_validator_operation_display

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
            self.workflow_breadcrumb_item(
                workflow,
                url=reverse_with_org(
                    "workflows:workflow_detail",
                    request=self.request,
                    kwargs={"pk": workflow.pk},
                ),
            ),
        )
        breadcrumbs.append({"name": _("Validations"), "url": ""})
        return breadcrumbs


class WorkflowStepAssertionModalBase(WorkflowStepAssertionsMixin, FormView):
    template_name = "workflows/partials/assertion_form.html"
    form_class = RulesetAssertionForm

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        requested_stage = self._requested_tabular_stage()
        catalog_stage = "input" if requested_stage else None
        kwargs["catalog_choices"] = self.get_catalog_choices(catalog_stage)
        kwargs["catalog_entries"] = getattr(self, "_catalog_entries_cache", [])
        kwargs["validator"] = self.step.validator
        kwargs["target_slug_datalist_id"] = self.get_target_slug_datalist_id()
        kwargs["workflow_signal_names"] = getattr(
            self,
            "_workflow_signal_names_cache",
            set(),
        )
        kwargs["shacl_sparql_assertion_count"] = self.get_shacl_sparql_assertion_count()
        kwargs["tabular_columns"] = self._get_tabular_columns()
        kwargs["tabular_column_types"] = {
            field["name"]: field["type"] for field in self._get_tabular_fields()
        }
        kwargs["requested_tabular_stage"] = requested_stage
        return kwargs

    def _requested_tabular_stage(self) -> str | None:
        """Return a valid stage requested by a Tabular assertion Add action."""
        validator = getattr(self.step, "validator", None)
        if getattr(validator, "validation_type", None) != ValidationType.TABULAR:
            return None
        stage = self.request.GET.get("tabular_stage")
        if stage in {"dataset", "row", "column"}:
            return stage
        get_assertion = getattr(self, "_get_assertion", None)
        if callable(get_assertion):
            stored_stage = (get_assertion().options or {}).get("tabular_stage")
            if stored_stage in {"dataset", "row", "column"}:
                return stored_stage
        return None

    def _get_tabular_columns(self) -> set[str]:
        """Declared column names for a Tabular Validator step (else empty).

        Parsed from the step's stored Table Schema (``ruleset.rules_text``) so
        the assertion form can reject a ``row.<column>`` reference to a column
        that isn't declared. A missing/malformed schema yields an empty set,
        which the form treats as "can't check" rather than an error.
        """
        return {field["name"] for field in self._get_tabular_fields()}

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
        requested_stage = self._requested_tabular_stage()
        stage_labels = {
            "dataset": _("Dataset assertion"),
            "row": _("Row assertion"),
            "column": _("Column assertion"),
        }
        stage_guidance = {
            "dataset": _(
                "Runs once against dataset metadata before native column and "
                "row checks.",
            ),
            "row": _(
                "Runs for each row and is aggregated into a bounded finding.",
            ),
            "column": _(
                "Runs once after row validation against typed column aggregates.",
            ),
        }
        context.update(
            {
                "modal_title": getattr(self, "modal_title", _("Assertion")),
                "form_action": self.request.get_full_path(),
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
                "requested_tabular_stage": requested_stage,
                "tabular_stage_label": stage_labels.get(requested_stage, ""),
                "tabular_stage_guidance": stage_guidance.get(requested_stage, ""),
                "tabular_cel_assist": self._tabular_cel_assist(requested_stage),
            },
        )
        return context

    def _tabular_cel_assist(self, stage: str | None) -> dict[str, object] | None:
        """Return stage-aware completion data for the Tabular CEL editor."""
        if stage not in {"dataset", "row", "column"}:
            return None
        fields = self._get_tabular_fields()
        aliases: dict[str, list[str]] = {}
        for field in fields:
            candidate = re.sub(r"[^A-Za-z0-9_]", "_", field["name"])
            if candidate and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", candidate):
                aliases.setdefault(candidate, []).append(field["name"])
        columns = [
            {
                **field,
                "alias": next(
                    (
                        alias
                        for alias, names in aliases.items()
                        if names == [field["name"]]
                    ),
                    "",
                ),
            }
            for field in fields
        ]
        catalog = [
            {"value": value, "label": str(label)}
            for value, label in self.get_catalog_choices("input")
        ]
        return {"stage": stage, "columns": columns, "catalog": catalog}

    def _get_tabular_fields(self) -> list[dict[str, str]]:
        """Return declared Tabular fields in schema order for CEL assistance."""
        validator = getattr(self.step, "validator", None)
        if getattr(validator, "validation_type", None) != ValidationType.TABULAR:
            return []
        ruleset = getattr(self.step, "ruleset", None)
        raw_schema = getattr(ruleset, "rules_text", "") or ""
        if not raw_schema:
            return []
        import json

        from validibot.validations.validators.tabular.schema import parse_table_schema

        try:
            schema = parse_table_schema(json.loads(raw_schema))
        except (ValueError, TypeError):
            return []
        return [{"name": field.name, "type": field.type} for field in schema.fields]

    def get_shacl_sparql_assertion_count(self) -> int:
        return (
            self.get_ruleset()
            .assertions.filter(
                assertion_type=AssertionType.SHACL,
            )
            .count()
        )


class WorkflowStepAssertionCreateView(WorkflowStepAssertionModalBase):
    modal_title = _("Add Assertion")
    submit_label = _("Add Assertion")

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if context.get("tabular_stage_label"):
            context["modal_title"] = _("Add %(stage)s") % {
                "stage": context["tabular_stage_label"],
            }
        return context

    def form_valid(self, form):
        ruleset = self.get_ruleset()
        try:
            assertion = AssertionMutationService.create_from_cleaned_data(
                ruleset=ruleset,
                cleaned_data=form.cleaned_data,
            )
        except ValidationError as exc:
            _attach_validation_error(form, exc)
            return self.form_invalid(form)
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

    def get_shacl_sparql_assertion_count(self) -> int:
        return (
            self.get_ruleset()
            .assertions.filter(assertion_type=AssertionType.SHACL)
            .exclude(pk=self._get_assertion().pk)
            .count()
        )

    def form_valid(self, form):
        assertion = self._get_assertion()
        try:
            AssertionMutationService.update_from_cleaned_data(
                assertion=assertion,
                cleaned_data=form.cleaned_data,
            )
        except ValidationError as exc:
            _attach_validation_error(form, exc)
            return self.form_invalid(form)
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
        try:
            AssertionMutationService.delete(assertion=assertion)
        except ValidationError as exc:
            error_message = _validation_error_message(exc)
            messages.error(request, error_message)
            if request.headers.get("HX-Request"):
                return hx_trigger_response(
                    status_code=400,
                    level="error",
                    message=error_message,
                    close_modal=None,
                    include_steps_changed=False,
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
        validator = getattr(self.step, "validator", None)
        use_stage_buckets = bool(validator and validator.has_processor)
        use_tabular_stage_buckets = bool(
            validator and validator.validation_type == ValidationType.TABULAR,
        )
        moved = AssertionMutationService.move(
            ruleset=ruleset,
            assertion=assertion,
            direction=direction,
            use_stage_buckets=use_stage_buckets,
            use_tabular_stage_buckets=use_tabular_stage_buckets,
        )
        if not moved:
            return hx_trigger_response(status_code=204)
        return hx_trigger_response(
            message=_("Assertion moved."),
            extra_payload={
                "assertions-changed": {
                    "focus_assertion_id": assertion.pk,
                },
            },
        )


def _attach_validation_error(form: RulesetAssertionForm, exc: ValidationError) -> None:
    """Attach model/service validation errors to an unbound form shape."""

    if hasattr(exc, "message_dict"):
        for field, field_messages in exc.message_dict.items():
            form_field = field if field in form.fields else None
            for message in field_messages:
                form.add_error(form_field, message)
        return
    form.add_error(None, exc)


def _validation_error_message(exc: ValidationError) -> str:
    """Return a plain text summary for toast responses."""

    return " ".join(str(message) for message in exc.messages) or str(exc)


class WorkflowStepAssertionsPartialView(WorkflowObjectMixin, TemplateView):
    """HTMx partial: re-render just the assertions editor content.

    Called via ``hx-trigger="assertions-changed from:body"`` after
    assertion create, update, delete, or reorder operations.  Returns
    the assertions area HTML without a full page reload.
    """

    template_name = "workflows/partials/assertions_editor_content.html"

    def dispatch(self, request, *args, **kwargs):
        self.step = get_object_or_404(
            WorkflowStep,
            workflow=self.get_workflow(),
            pk=self.kwargs.get("step_id"),
        )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        workflow = self.get_workflow()
        validator = self.step.validator
        allow_assertions = validator and validator.supports_assertions

        ruleset = None
        assertions = []
        if allow_assertions:
            ruleset = self.step.ruleset or ensure_advanced_ruleset(
                workflow,
                self.step,
                validator,
            )
            assertions = list(ruleset.assertions.all().order_by("order", "pk"))

        grouped_assertions = {"input": [], "output": []}
        for assertion in assertions:
            stage = assertion.resolved_run_stage
            key = "input" if stage == CatalogRunStage.INPUT else "output"
            grouped_assertions[key].append(assertion)

        from validibot.workflows.views.steps import _step_has_signal_stages

        uses_signal_stages = bool(
            validator and _step_has_signal_stages(self.step) and allow_assertions,
        )
        uses_tabular_stages = bool(
            validator
            and validator.validation_type == ValidationType.TABULAR
            and allow_assertions,
        )
        tabular_assertion_groups = {
            "dataset": [],
            "row": [],
            "column": [],
        }
        for assertion in assertions:
            stage = (assertion.options or {}).get("tabular_stage", "dataset")
            group = tabular_assertion_groups.get(
                stage,
                tabular_assertion_groups["dataset"],
            )
            group.append(assertion)
        validator_operation = get_validator_operation_display(validator)
        default_assertions_count = (
            validator.default_ruleset.assertions.count()
            if validator and validator.default_ruleset_id
            else 0
        )

        context.update(
            {
                "workflow": workflow,
                "step": self.step,
                "validator": validator,
                "assertions": assertions,
                "assertion_groups": grouped_assertions,
                "uses_signal_stages": uses_signal_stages,
                "uses_tabular_stages": uses_tabular_stages,
                "tabular_assertion_groups": tabular_assertion_groups,
                "validator_operation": validator_operation,
                "can_manage_assertions": self.user_can_manage_workflow()
                and allow_assertions,
                "supports_assertions": allow_assertions,
                "validator_default_assertions_count": default_assertions_count,
            },
        )
        return context
