"""Step management views including the wizard and editing.

Contains the step list, add-step wizard, step form (create/update),
step edit detail page, template variable editing, display signal
selection, and step reordering/deletion. Also includes helper functions
for FMU signal-stage resolution.
"""

import json
import logging
from http import HTTPStatus

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import models
from django.db import transaction
from django.http import Http404
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.shortcuts import render
from django.template.loader import render_to_string
from django.utils.translation import gettext_lazy as _
from django.views import View
from django.views.generic import TemplateView
from django.views.generic.edit import FormView

from validibot.actions.constants import ActionCategoryType
from validibot.actions.constants import CredentialActionType
from validibot.actions.models import ActionDefinition
from validibot.actions.models import SlackMessageAction
from validibot.actions.registry import get_action_form
from validibot.core.utils import reverse_with_org
from validibot.core.view_helpers import hx_trigger_response
from validibot.submissions.constants import SubmissionDataFormat
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import BindingSourceScope
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import ValidationType
from validibot.validations.constants import ValidatorReleaseState
from validibot.validations.constants import XMLSchemaType
from validibot.validations.models import SignalDefinition
from validibot.validations.models import StepSignalBinding
from validibot.validations.models import Validator
from validibot.workflows.forms import SignalBindingEditForm
from validibot.workflows.forms import WorkflowStepTypeForm
from validibot.workflows.forms import get_config_form_class
from validibot.workflows.mixins import WorkflowObjectMixin
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep
from validibot.workflows.views.management import MAX_STEP_COUNT
from validibot.workflows.views_helpers import ensure_advanced_ruleset
from validibot.workflows.views_helpers import resequence_workflow_steps
from validibot.workflows.views_helpers import save_workflow_action_step
from validibot.workflows.views_helpers import save_workflow_step

logger = logging.getLogger(__name__)

CREDENTIAL_PLACEMENT_GUIDANCE = _(
    "Signed credential steps must come after all validation steps and "
    "blocking actions.",
)
CREDENTIAL_PLACEMENT_FOLLOWUP = _(
    "Advisory actions may appear after the signed credential step.",
)
CREDENTIAL_MOVE_GUIDANCE = _(
    "Move buttons that would break this rule are disabled.",
)


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
                if vtype == ValidationType.XML_SCHEMA:
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
                step.is_signed_credential_step = _is_signed_credential_step(step)
                if not config and variant:
                    if isinstance(variant, SlackMessageAction):
                        config["message"] = variant.message
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
                    if key not in {"message"}
                }
                step.action_summary = {
                    "message": config.get("message"),
                    "extras": extras,
                }
            step.config = config
        has_credential_step = _annotate_reorder_controls(steps)
        show_private_notes = self.user_can_manage_workflow()
        context = {
            "workflow": workflow,
            "steps": steps,
            "max_step_count": MAX_STEP_COUNT,
            "show_private_notes": show_private_notes,
            "can_view_workflow": self.user_can_view_workflow(),
            "can_manage_workflow": self.user_can_manage_workflow(),
            "can_launch_workflow": workflow.can_execute(user=request.user),
            "credential_ordering_guidance": (
                {
                    "headline": str(CREDENTIAL_PLACEMENT_GUIDANCE),
                    "followup": str(CREDENTIAL_PLACEMENT_FOLLOWUP),
                    "move_note": str(CREDENTIAL_MOVE_GUIDANCE),
                }
                if has_credential_step
                else None
            ),
        }
        return render(request, self.template_name, context)


class WorkflowStepWizardView(WorkflowObjectMixin, View):
    """Present the validator selector in the add-step modal."""

    template_select = "workflows/partials/workflow_step_wizard_select.html"

    def dispatch(self, request, *args, **kwargs):
        if not request.headers.get("HX-Request"):
            return HttpResponse(status=400)
        return super().dispatch(request, *args, **kwargs)

    def _get_insert_after_step(self, request) -> int | None:
        """Extract and validate the insert_after_step parameter.

        This is the PK of the step to insert after. Using the step ID
        (rather than order value) is robust against concurrent
        resequencing operations.
        """
        raw = request.GET.get("insert_after_step") or request.POST.get(
            "insert_after_step"
        )
        if raw is None:
            return None
        try:
            return int(raw)
        except (ValueError, TypeError):
            return None

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
        insert_after_step = self._get_insert_after_step(request)

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
            if insert_after_step is not None:
                create_url += f"?insert_after_step={insert_after_step}"
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

        Excludes DRAFT validators (not ready for display). COMING_SOON validators
        are included but will be disabled in the UI.
        """
        validators: list[Validator] = []
        for validator in (
            Validator.objects.filter(
                models.Q(org__isnull=True) | models.Q(org=workflow.org),
                is_enabled=True,
            )
            .exclude(
                release_state=ValidatorReleaseState.DRAFT,
            )
            .order_by("validation_type", "name", "pk")
        ):
            self._ensure_validator_defaults(validator)
            validators.append(validator)
        return validators

    def _available_action_definitions(self) -> list[ActionDefinition]:
        """Return action definitions the current user can add.

        Filters out definitions whose ``required_commercial_feature`` is not
        enabled
        and whose action plugins are not registered in the current
        process.
        """
        from validibot.core.features import is_feature_enabled

        definitions = ActionDefinition.objects.filter(is_active=True).order_by(
            "action_category",
            "name",
        )
        return [
            d
            for d in definitions
            if (
                get_action_form(d.type) is not None
                and (
                    not d.required_commercial_feature
                    or is_feature_enabled(d.required_commercial_feature)
                )
            )
        ]

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
            "insert_after_step": self._get_insert_after_step(request),
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
                    ValidationType.FMU,
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
        credential_entries = [
            self._serialize_action_definition(defn)
            for defn in action_definitions
            if defn.action_category == ActionCategoryType.CREDENTIAL
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
                "slug": "credentials",
                "label": str(_("Credentials")),
                "entries": credential_entries,
            },
        )
        options.extend(integration_entries)
        options.extend(credential_entries)

        return tabs, options

    def _ensure_validator_defaults(self, validator: Validator) -> None:
        """
        Backfill expected supported formats/file types for validators created
        before defaults expanded (notably FMU, which now accepts JSON/TEXT).
        """
        if validator.validation_type != ValidationType.FMU:
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
        is_disabled = False

        # Check if coming soon (takes precedence over compatibility)
        if validator.is_coming_soon:
            is_disabled = True
            disabled_reason = _("Coming soon")
        elif not is_compatible:
            is_disabled = True
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
            "disabled": is_disabled,
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
        """Return validators that can be selected for workflow steps.

        Only PUBLISHED validators can be used in workflows. DRAFT and
        COMING_SOON validators are excluded.
        """
        from django.db.models import Q

        workflow = self.get_workflow()
        return Validator.objects.filter(
            Q(is_system=True) | Q(org=workflow.org),
            release_state=ValidatorReleaseState.PUBLISHED,
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
        """Look up the ActionDefinition for create or update mode.

        For create mode, also enforces ``required_commercial_feature`` gating so
        that Pro-only actions cannot be added to a workflow when the
        required commercial package is not installed.  This is the
        server-side companion to the UI filtering in
        ``_available_action_definitions()`` — both are necessary for
        defense in depth.
        """
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
                # Server-side enforcement: reject action types whose
                # required commercial feature is not enabled.
                required = self._action_definition.required_commercial_feature
                if required:
                    from validibot.core.features import is_feature_enabled

                    if not is_feature_enabled(required):
                        raise Http404
                if get_action_form(self._action_definition.type) is None:
                    raise Http404
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
        else:
            # Pass org and validator for forms that need them (e.g., EnergyPlus)
            workflow = self.get_workflow()
            kwargs["org"] = workflow.org
            kwargs["validator"] = self.get_validator()
        return kwargs

    def _get_insert_after_step(self) -> int | None:
        """Read the insert_after_step query param for mid-list insertion."""
        raw = self.request.GET.get("insert_after_step")
        if raw is None:
            return None
        try:
            return int(raw)
        except (ValueError, TypeError):
            return None

    def form_valid(self, form):
        workflow = self.get_workflow()
        insert_after_step = (
            self._get_insert_after_step() if self.mode == "create" else None
        )
        if self.is_action_step():
            definition = self.get_action_definition()
            saved_step = save_workflow_action_step(
                workflow,
                definition,
                form,
                step=self.get_step(),
                insert_after_step=insert_after_step,
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
                insert_after_step=insert_after_step,
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
        detail_url = reverse_with_org(
            "workflows:workflow_detail",
            request=self.request,
            kwargs={"pk": workflow.pk},
        )
        if hasattr(self, "saved_step") and self.saved_step:
            validator = self.saved_step.validator
            supports_assertions = validator and validator.supports_assertions
            # Schema-only validators don't support assertions — skip
            # the step edit screen and return to the workflow detail.
            if not supports_assertions:
                return f"{detail_url}#workflow-steps-col"

            anchor = (
                "#workflow-step-assertions"
                if supports_assertions
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
                    and step.validator.supports_assertions,
                ),
                "credential_step_guidance": self._get_credential_step_guidance(),
            },
        )
        return context

    def _get_credential_step_guidance(self) -> dict[str, str] | None:
        """Return UI guidance for signed credential action steps."""

        if not self.is_action_step():
            return None
        definition = self.get_action_definition()
        if definition.type != CredentialActionType.SIGNED_CREDENTIAL:
            return None

        summary = str(
            _(
                "When this step is present, the workflow editor keeps it after "
                "all validation steps and blocking actions."
            )
        )
        if self.mode == "create":
            status = str(
                _(
                    "New steps are added at the end of the workflow. You can "
                    "still place advisory actions after this one later."
                )
            )
        else:
            step = self.get_step()
            workflow = self.get_workflow()
            step_number = step.step_number_display if step else "?"
            status = str(
                _("You are editing %(step)s in a workflow with %(count)s steps.")
                % {
                    "step": step_number,
                    "count": workflow.steps.count(),
                }
            )

        return {
            "headline": str(CREDENTIAL_PLACEMENT_GUIDANCE),
            "followup": str(CREDENTIAL_PLACEMENT_FOLLOWUP),
            "status": status,
            "summary": summary,
        }

    def _has_dedicated_step_edit_view(self) -> bool:
        """Return whether the step has a distinct overview page.

        Action steps use the settings form as their only edit surface.
        Their ``workflow_step_edit`` route redirects back to the same
        settings page, so breadcrumbs should not include a self-link for
        the step number.
        """

        return not self.is_action_step()

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
            if self._has_dedicated_step_edit_view():
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
            else:
                breadcrumbs.append(
                    {
                        "name": _("%(step)s: Edit Step Detail")
                        % {"step": step.step_number_display},
                        "url": "",
                    },
                )
        return breadcrumbs


# ── Signal-stage helpers ─────────────────────────────────────────────
# These helpers let the step detail view detect signal stages and group
# assertions correctly.


def _step_has_signal_stages(step) -> bool:
    """True when the step's validator has a processor (input → process → output).

    This checks the validator's *capability* (``has_processor``), not whether
    signal definitions actually exist yet.  A validator with a processor
    always shows the three-section layout (input assertions, process divider,
    output assertions) even when no signals have been defined yet, so the
    user sees the structural slots where signals will appear.
    """
    validator = getattr(step, "validator", None)
    return bool(validator and validator.has_processor)


def _resolve_assertion_stage(assertion) -> CatalogRunStage:
    """Determine which stage bucket an assertion belongs to.

    Delegates to the model's ``resolved_run_stage`` property which checks
    ``target_signal_definition``, then defaults to OUTPUT.
    """
    return assertion.resolved_run_stage


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
        allow_assertions = validator and validator.supports_assertions
        if allow_assertions:
            ruleset = self.step.ruleset or ensure_advanced_ruleset(
                workflow,
                self.step,
                validator,
            )
            assertions = list(ruleset.assertions.all().order_by("order", "pk"))
        # Group assertions by run stage so input/output sections render
        # separately in the step detail template.
        grouped_assertions = {
            "input": [],
            "output": [],
        }
        for assertion in assertions:
            stage = _resolve_assertion_stage(assertion)
            key = "input" if stage == CatalogRunStage.INPUT else "output"
            grouped_assertions[key].append(assertion)
        uses_signal_stages = bool(
            validator and _step_has_signal_stages(self.step) and allow_assertions,
        )
        default_assertions_count = (
            validator.default_ruleset.assertions.count()
            if validator and validator.default_ruleset_id
            else 0
        )

        # Build unified input/output signals for the right-column card.
        from validibot.workflows.views_helpers import (
            build_unified_signals_from_definitions,
        )

        unified_signals = build_unified_signals_from_definitions(self.step)

        # Workflow-level signal mappings — shown as "Available Signals"
        # on every step editor so authors know what s.name values exist.
        from validibot.workflows.models import WorkflowSignalMapping

        available_signals = list(
            WorkflowSignalMapping.objects.filter(
                workflow=workflow,
            )
            .order_by("position")
            .values("name", "source_path"),
        )

        # Promoted outputs from upstream steps — these also appear
        # in the s.* namespace, so authors need to see them alongside
        # workflow-level mappings.
        from validibot.validations.models import SignalDefinition

        promoted_outputs = list(
            SignalDefinition.objects.filter(
                workflow_step__workflow=workflow,
                workflow_step__order__lt=self.step.order,
            )
            .exclude(signal_name="")
            .values_list("signal_name", "contract_key")
        )
        for signal_name, contract_key in promoted_outputs:
            available_signals.append(
                {
                    "name": signal_name,
                    "source_path": f"(promoted from {contract_key})",
                }
            )

        # Upstream step outputs — shown so authors know what
        # steps.<key>.output.<name> paths are available.
        from validibot.workflows.models import WorkflowStep

        upstream_outputs = []
        for ws in (
            WorkflowStep.objects.filter(
                workflow=workflow,
                order__lt=self.step.order,
            )
            .exclude(pk=self.step.pk)
            .order_by("order")
        ):
            from validibot.validations.constants import SignalDirection
            from validibot.validations.models import SignalDefinition

            outputs = list(
                SignalDefinition.objects.filter(
                    workflow_step=ws,
                    direction=SignalDirection.OUTPUT,
                )
                .union(
                    SignalDefinition.objects.filter(
                        validator=ws.validator,
                        direction=SignalDirection.OUTPUT,
                    )
                )
                .values_list("contract_key", flat=True),
            )
            if outputs:
                step_key = ws.step_key or str(ws.pk)
                upstream_outputs.append(
                    {
                        "step_name": ws.name,
                        "step_key": step_key,
                        "outputs": [
                            f"steps.{step_key}.output.{name}" for name in outputs
                        ],
                    },
                )

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
                "catalog_tab_prefix": f"workflow-step-{self.step.pk}-catalog",
                "validator_default_assertions_count": default_assertions_count,
                "can_manage_workflow": self.user_can_manage_workflow(),
                "unified_signals": unified_signals,
                "available_signals": available_signals,
                "upstream_outputs": upstream_outputs,
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


class WorkflowStepTemplateVariablesView(WorkflowObjectMixin, FormView):
    """HTMx endpoint for editing template variable annotations.

    Renders and processes the template variables card that appears in
    the step detail page's right column.  This view is declared as the
    ``view_class`` in the EnergyPlus ``StepEditorCardSpec``.

    GET returns the rendered card partial (for initial page load).
    POST validates the form, merges annotations into ``step.config``,
    and returns the re-rendered card with a toast trigger.
    """

    template_name = "workflows/partials/template_variables_card.html"

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)
        self.step = get_object_or_404(
            WorkflowStep,
            workflow=self.get_workflow(),
            pk=self.kwargs.get("step_id"),
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        from validibot.workflows.forms import TemplateVariableAnnotationForm

        return TemplateVariableAnnotationForm(
            data=self.request.POST or None,
            step=self.step,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        step_config = self.step.config or {}
        context.update(
            {
                "workflow": self.get_workflow(),
                "step": self.step,
                "tplvar_form": context.get("form"),
                "display_signals": step_config.get("display_signals", []),
            },
        )
        return context

    def form_valid(self, form):
        from validibot.workflows.views_helpers import save_template_variable_annotations

        save_template_variable_annotations(form)

        if self.request.headers.get("HX-Request"):
            # Re-render the card with updated data and a toast trigger
            context = self.get_context_data(form=self.get_form())
            html = render_to_string(
                self.template_name,
                context,
                request=self.request,
            )
            response = HttpResponse(html)
            response["HX-Trigger"] = json.dumps(
                {
                    "showToast": {
                        "message": str(
                            _("Template variable annotations saved."),
                        ),
                        "level": "success",
                    },
                },
            )
            return response

        return HttpResponseRedirect(
            reverse_with_org(
                "workflows:workflow_step_edit",
                request=self.request,
                kwargs={
                    "pk": self.get_workflow().pk,
                    "step_id": self.step.pk,
                },
            ),
        )

    def form_invalid(self, form):
        context = self.get_context_data(form=form)
        return render(self.request, self.template_name, context)


class WorkflowStepDisplaySignalsView(WorkflowObjectMixin, FormView):
    """HTMx modal endpoint for editing which output signals are shown to users.

    GET returns the modal form content (loaded into displaySignalsModal).
    POST validates and saves the selection to ``step.config["display_signals"]``,
    then triggers a page reload via HX-Trigger.
    """

    template_name = "workflows/partials/display_signals_modal_content.html"

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)
        self.step = get_object_or_404(
            WorkflowStep,
            workflow=self.get_workflow(),
            pk=self.kwargs.get("step_id"),
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        from validibot.workflows.forms import DisplaySignalsForm

        return DisplaySignalsForm(
            data=self.request.POST or None,
            step=self.step,
            validator=self.step.validator,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "workflow": self.get_workflow(),
                "step": self.step,
            },
        )
        return context

    def form_valid(self, form):
        selected = form.cleaned_data.get("display_signals", [])
        config = dict(self.step.config or {})
        config["display_signals"] = selected
        self.step.config = config
        self.step.save(update_fields=["config"])

        response = HttpResponse(status=HTTPStatus.NO_CONTENT)
        response["HX-Trigger"] = json.dumps(
            {
                "close-modal": "displaySignalsModal",
                "showToast": {
                    "message": str(_("Display signals updated.")),
                    "level": "success",
                },
            },
        )
        response["HX-Refresh"] = "true"
        return response

    def form_invalid(self, form):
        context = self.get_context_data(form=form)
        return render(self.request, self.template_name, context)


class WorkflowStepToggleDisplaySignalView(WorkflowObjectMixin, View):
    """HTMx endpoint that toggles a single output signal's visibility.

    POST adds or removes the signal slug from the step's
    ``config["display_signals"]`` list and returns the updated toggle
    button HTML fragment.

    Semantics: an empty ``display_signals`` list means "show all".
    Toggling a signal OFF when the list is empty first populates the
    list with all output slugs, then removes the target slug.
    """

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)
        self.step = get_object_or_404(
            WorkflowStep,
            workflow=self.get_workflow(),
            pk=self.kwargs.get("step_id"),
        )
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        slug = self.kwargs["signal_slug"]
        config = dict(self.step.config or {})
        display_signals = list(config.get("display_signals", []))

        from validibot.validations.constants import SignalDirection

        owner_filter = models.Q(workflow_step=self.step)
        if self.step.validator_id:
            owner_filter |= models.Q(validator=self.step.validator)

        all_slugs = list(
            SignalDefinition.objects.filter(
                owner_filter,
                direction=SignalDirection.OUTPUT,
            ).values_list("contract_key", flat=True),
        )

        if not display_signals:
            # Empty list means "show all". Expand to the explicit list
            # so we can remove the target slug.
            display_signals = list(all_slugs)

        if slug in display_signals:
            display_signals.remove(slug)
        else:
            display_signals.append(slug)

        # If the result matches "all shown", normalize back to empty
        # list to preserve the "show all" semantic.
        if set(display_signals) == set(all_slugs):
            display_signals = []

        config["display_signals"] = display_signals
        self.step.config = config
        self.step.save(update_fields=["config"])

        is_shown = not display_signals or slug in display_signals
        return HttpResponse(
            render_to_string(
                "workflows/partials/signal_show_toggle.html",
                {
                    "signal_slug": slug,
                    "is_shown": is_shown,
                    "step": self.step,
                    "workflow": self.get_workflow(),
                },
                request=request,
            ),
        )


class WorkflowStepTemplateVariableEditView(WorkflowObjectMixin, FormView):
    """HTMx modal endpoint for editing a single template variable's annotations.

    Replaces the old "save all at once" template variables form with a
    per-variable modal pattern.  The variable is identified by its
    ``SignalDefinition`` row.

    GET returns the modal form content for the specified variable.
    POST validates and saves annotations for that single variable,
    then triggers a page reload.
    """

    template_name = "workflows/partials/template_variable_edit_modal_content.html"

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            return HttpResponse(status=HTTPStatus.FORBIDDEN)
        self.step = get_object_or_404(
            WorkflowStep,
            workflow=self.get_workflow(),
            pk=self.kwargs.get("step_id"),
        )
        self.var_index = int(self.kwargs.get("var_index", 0))

        # Look up the template signal by index in the same ordering used
        # by the annotation form and signal display.
        from validibot.validations.constants import SignalOriginKind
        from validibot.validations.models import StepSignalBinding

        bindings = list(
            StepSignalBinding.objects.filter(
                workflow_step=self.step,
                signal_definition__origin_kind=SignalOriginKind.TEMPLATE,
            )
            .select_related("signal_definition")
            .order_by(
                "signal_definition__order",
                "signal_definition__contract_key",
            )
        )
        if self.var_index < 0 or self.var_index >= len(bindings):
            return HttpResponse(status=HTTPStatus.NOT_FOUND)

        binding = bindings[self.var_index]
        sig = binding.signal_definition
        meta = sig.metadata or {}
        default_val = binding.default_value
        self.variable = {
            "name": sig.native_name or sig.contract_key,
            "description": sig.label or "",
            "default": str(default_val) if default_val is not None else "",
            "units": sig.unit or "",
            "variable_type": meta.get("variable_type", "text"),
            "min_value": meta.get("min_value"),
            "min_exclusive": meta.get("min_exclusive", False),
            "max_value": meta.get("max_value"),
            "max_exclusive": meta.get("max_exclusive", False),
            "choices": meta.get("choices", []),
        }
        self._signal_pk = sig.pk
        self._binding_pk = binding.pk
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        from validibot.workflows.forms import SingleTemplateVariableForm

        return SingleTemplateVariableForm(
            data=self.request.POST or None,
            variable=self.variable,
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update(
            {
                "workflow": self.get_workflow(),
                "step": self.step,
                "variable": self.variable,
                "var_index": self.var_index,
            },
        )
        return context

    def form_valid(self, form):
        from validibot.validations.models import SignalDefinition
        from validibot.validations.models import StepSignalBinding
        from validibot.validations.signal_metadata.metadata import (
            TemplateSignalMetadata,
        )
        from validibot.workflows.views_helpers import _parse_choices
        from validibot.workflows.views_helpers import _parse_optional_float

        variable_type = form.cleaned_data.get("variable_type", "text")
        metadata = TemplateSignalMetadata(
            variable_type=variable_type,
            min_value=_parse_optional_float(
                form.cleaned_data.get("min_value", ""),
            ),
            min_exclusive=form.cleaned_data.get("min_exclusive", False),
            max_value=_parse_optional_float(
                form.cleaned_data.get("max_value", ""),
            ),
            max_exclusive=form.cleaned_data.get("max_exclusive", False),
            choices=_parse_choices(
                form.cleaned_data.get("choices", ""),
            ),
        ).model_dump()

        SignalDefinition.objects.filter(pk=self._signal_pk).update(
            label=form.cleaned_data.get("description", ""),
            unit=form.cleaned_data.get("units", ""),
            metadata=metadata,
            provider_binding={"variable_type": variable_type},
        )
        default_val = form.cleaned_data.get("default", "")
        StepSignalBinding.objects.filter(pk=self._binding_pk).update(
            default_value=default_val if default_val else None,
            is_required=not bool(default_val),
        )

        response = HttpResponse(status=HTTPStatus.NO_CONTENT)
        response["HX-Trigger"] = json.dumps(
            {
                "close-modal": "templateVariableEditModal",
                "showToast": {
                    "message": str(
                        _("Variable %(name)s updated.")
                        % {"name": f"${self.variable['name']}"},
                    ),
                    "level": "success",
                },
            },
        )
        response["HX-Refresh"] = "true"
        return response

    def form_invalid(self, form):
        context = self.get_context_data(form=form)
        return render(self.request, self.template_name, context)


class WorkflowStepSignalEditView(WorkflowObjectMixin, FormView):
    """Edit a signal definition and its binding via an HTMx modal.

    Handles both step-owned signals (all fields editable) and library-owned
    signals (definition fields read-only, binding fields editable).

    The signal must belong to either the current step (step-owned) or the
    step's validator (library-owned). This prevents editing signals from
    other steps or workflows.

    Uses WorkflowObjectMixin (not WorkflowStepAssertionsMixin) because
    signal editing doesn't require assertion support — that mixin
    would reject steps whose validators don't support assertions,
    which is unrelated.
    """

    template_name = "workflows/partials/signal_edit_modal_content.html"
    form_class = SignalBindingEditForm

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        # Resolve the step early so _get_signal()/_get_binding() can use it.
        self.step = get_object_or_404(
            WorkflowStep,
            workflow=self.get_workflow(),
            pk=self.kwargs.get("step_id"),
        )
        return super().dispatch(request, *args, **kwargs)

    def _get_signal(self):
        """Fetch the signal definition, scoped to this step or its validator."""
        if hasattr(self, "_signal"):
            return self._signal
        signal_id = self.kwargs.get("signal_id")
        # Scope the lookup: signal must belong to this step or its validator.
        sig = (
            SignalDefinition.objects.filter(pk=signal_id)
            .filter(
                models.Q(workflow_step=self.step)
                | models.Q(validator=self.step.validator),
            )
            .first()
        )
        if not sig:
            raise Http404
        self._signal = sig
        return sig

    def _get_binding(self):
        """Get or create the per-step binding for this signal."""
        if hasattr(self, "_binding"):
            return self._binding
        sig = self._get_signal()
        self._binding, _ = StepSignalBinding.objects.get_or_create(
            workflow_step=self.step,
            signal_definition=sig,
            defaults={
                "source_scope": BindingSourceScope.SUBMISSION_PAYLOAD,
                "source_data_path": sig.native_name or "",
                "is_required": True,
            },
        )
        return self._binding

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["signal_definition"] = self._get_signal()
        kwargs["binding"] = self._get_binding()
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        sig = self._get_signal()
        from validibot.validations.constants import SignalDirection

        is_input = sig.direction == SignalDirection.INPUT
        title_prefix = _("Edit input") if is_input else _("Edit output")
        context.update(
            {
                "signal": sig,
                "binding": self._get_binding(),
                "is_library_signal": bool(sig.validator_id),
                "is_path_editable": sig.is_path_editable,
                "source_kind_display": sig.get_source_kind_display(),
                "modal_title": f"{title_prefix}: {sig.label or sig.contract_key}",
            },
        )

        # Build source path suggestions for the datalist: workflow-level
        # signals (s.name) so the user can easily bind an input to a signal.
        if is_input:
            from validibot.workflows.models import WorkflowSignalMapping

            workflow = self.get_workflow()
            signal_suggestions = [
                {
                    "value": f"s.{m['name']}",
                    "label": m["source_path"],
                }
                for m in WorkflowSignalMapping.objects.filter(
                    workflow=workflow,
                )
                .order_by("position")
                .values("name", "source_path")
            ]
            context["source_path_suggestions"] = signal_suggestions

        return context

    def form_valid(self, form):
        form.save()
        messages.success(self.request, _("Signal updated."))
        response = HttpResponse(status=200)
        response["HX-Refresh"] = "true"
        return response

    def render_to_response(self, context, **response_kwargs):
        if self.request.headers.get("HX-Request"):
            return render(
                self.request,
                self.template_name,
                context,
                status=response_kwargs.get("status", 200),
            )
        return super().render_to_response(context, **response_kwargs)


class WorkflowStepSignalAutoLinkView(WorkflowObjectMixin, View):
    """POST: auto-link an input signal to a matching workflow-level signal.

    Looks for a ``WorkflowSignalMapping`` whose ``name`` matches the
    signal definition's ``contract_key``.  When found, sets the
    ``StepSignalBinding.source_data_path`` to ``s.<name>`` and refreshes
    the page.  When no match exists, adds a warning message.
    """

    def dispatch(self, request, *args, **kwargs):
        if not self.user_can_manage_workflow():
            raise PermissionDenied
        self.step = get_object_or_404(
            WorkflowStep,
            workflow=self.get_workflow(),
            pk=self.kwargs.get("step_id"),
        )
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        from validibot.workflows.models import WorkflowSignalMapping

        workflow = self.get_workflow()
        signal_id = self.kwargs.get("signal_id")

        # Scope lookup: signal must belong to this step or its validator.
        signal_def = (
            SignalDefinition.objects.filter(pk=signal_id)
            .filter(
                models.Q(workflow_step=self.step)
                | models.Q(validator=self.step.validator),
            )
            .first()
        )
        if not signal_def:
            raise Http404

        contract_key = signal_def.contract_key

        mapping = WorkflowSignalMapping.objects.filter(
            workflow=workflow,
            name=contract_key,
        ).first()

        if not mapping:
            messages.warning(
                request,
                _(
                    "No matching workflow signal found. "
                    "Create a signal named '%(name)s' first."
                )
                % {"name": contract_key},
            )
            response = HttpResponse(status=HTTPStatus.OK)
            response["HX-Refresh"] = "true"
            return response

        binding, created = StepSignalBinding.objects.get_or_create(
            workflow_step=self.step,
            signal_definition=signal_def,
            defaults={
                "source_scope": BindingSourceScope.SIGNAL,
                "source_data_path": mapping.name,
                "is_required": True,
            },
        )
        if not created:
            binding.source_scope = BindingSourceScope.SIGNAL
            binding.source_data_path = mapping.name
            binding.save(update_fields=["source_scope", "source_data_path"])

        messages.success(
            request,
            _("Linked '%(key)s' to signal s.%(name)s.")
            % {"key": contract_key, "name": mapping.name},
        )
        response = HttpResponse(status=HTTPStatus.OK)
        response["HX-Refresh"] = "true"
        return response


class WorkflowStepOutputsPartialView(WorkflowObjectMixin, View):
    """GET: return the re-rendered validator outputs table partial.

    Used by HTMx to refresh just the outputs table after a promote/demote
    action, preserving the active tab state on the step detail page.
    """

    def get(self, request, *args, **kwargs):
        workflow = self.get_workflow()
        step = get_object_or_404(
            WorkflowStep,
            workflow=workflow,
            pk=self.kwargs.get("step_id"),
        )
        from validibot.workflows.views_helpers import (
            build_unified_signals_from_definitions,
        )

        unified_signals = build_unified_signals_from_definitions(step)
        return render(
            request,
            "workflows/partials/output_signals_table.html",
            {
                "unified_signals": unified_signals,
                "step": step,
                "workflow": workflow,
                "can_manage_workflow": self.user_can_manage_workflow(),
            },
        )


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
        try:
            step.delete()
        except models.ProtectedError:
            messages.warning(
                request,
                _(
                    "This step cannot be deleted because it has "
                    "existing validation runs. Remove the runs first."
                ),
            )
            response = HttpResponse(status=200)
            response["HX-Refresh"] = "true"
            return response
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
        # Validate credential step placement before persisting the
        # new order.  The clean() method on WorkflowStep only fires
        # on full_clean(), but the reorder uses raw .update() for
        # performance.  We check the proposed order here instead.
        placement_error = _validate_credential_step_order(steps)
        if placement_error:
            return hx_trigger_response(
                status_code=400,
                message=placement_error,
                level="warning",
            )

        with transaction.atomic():
            for pos, item in enumerate(steps, start=1):
                WorkflowStep.objects.filter(pk=item.pk).update(order=1000 + pos)
            resequence_workflow_steps(workflow)
        message = _("Workflow step order updated.")
        return hx_trigger_response(message, close_modal=None)


def _validate_credential_step_order(
    steps: list[WorkflowStep],
) -> str | None:
    """Check proposed step order for credential step placement violations.

    Returns an error message string if the proposed order violates the
    placement rules, or ``None`` if the order is valid.

    Rules:
        - All validator steps must appear before any credential step.
        - All BLOCKING action steps must appear before any credential step.
        - ADVISORY action steps may appear after the credential step.
    """
    from validibot.actions.constants import ActionFailureMode

    credential_index = None
    for i, step in enumerate(steps):
        if (
            step.action_id
            and step.action.definition_id
            and step.action.definition.type == CredentialActionType.SIGNED_CREDENTIAL
        ):
            credential_index = i
            break

    if credential_index is None:
        return None  # No credential step — no placement rules to check.

    # Check for validators or BLOCKING actions after the credential step.
    for step in steps[credential_index + 1 :]:
        if step.validator_id:
            return str(
                _("The signed credential step must come after all validation steps.")
            )
        if step.action_id and step.action.failure_mode == ActionFailureMode.BLOCKING:
            return str(
                _(
                    "The signed credential step must come after all "
                    "blocking action steps."
                )
            )

    return None


def _annotate_reorder_controls(steps: list[WorkflowStep]) -> bool:
    """Add move-button state to step objects for the workflow editor.

    The workflow detail page renders move up/down buttons inline. This helper
    simulates each move in memory and disables buttons that would violate the
    signed-credential placement rule, so authors get guidance before they click.
    """

    has_credential_step = False
    last_index = len(steps) - 1

    for index, step in enumerate(steps):
        step.move_up_disabled = index == 0
        step.move_down_disabled = index == last_index
        step.move_up_reason = str(_("This step is already first."))
        step.move_down_reason = str(_("This step is already last."))
        step.credential_ordering_hint = ""

        if _is_signed_credential_step(step):
            has_credential_step = True
            step.credential_ordering_hint = str(
                _(
                    "This step must remain after all validation steps and "
                    "blocking actions."
                )
            )

        if index > 0:
            proposed = list(steps)
            proposed[index - 1], proposed[index] = proposed[index], proposed[index - 1]
            placement_error = _validate_credential_step_order(proposed)
            if placement_error:
                step.move_up_disabled = True
                step.move_up_reason = placement_error

        if index < last_index:
            proposed = list(steps)
            proposed[index], proposed[index + 1] = proposed[index + 1], proposed[index]
            placement_error = _validate_credential_step_order(proposed)
            if placement_error:
                step.move_down_disabled = True
                step.move_down_reason = placement_error

    return has_credential_step


def _is_signed_credential_step(step: WorkflowStep) -> bool:
    """Return True when a workflow step is the signed credential action."""

    return bool(
        step.action_id
        and step.action
        and step.action.definition_id
        and step.action.definition.type == CredentialActionType.SIGNED_CREDENTIAL
    )
