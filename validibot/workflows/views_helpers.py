import logging
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from django import forms
from django.core.exceptions import ValidationError
from django.db import models
from django.http import Http404
from django.http import HttpRequest
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from rest_framework.request import Request

from validibot.actions.models import ActionDefinition
from validibot.projects.models import Project
from validibot.submissions.constants import SubmissionFileType
from validibot.submissions.models import detect_file_type
from validibot.users.models import Organization
from validibot.users.models import User
from validibot.users.permissions import PermissionCode
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import RulesetType
from validibot.validations.constants import ValidationType
from validibot.validations.constants import XMLSchemaType
from validibot.validations.models import Ruleset
from validibot.validations.models import Validator
from validibot.workflows.forms import AiAssistStepConfigForm
from validibot.workflows.forms import EnergyPlusStepConfigForm
from validibot.workflows.forms import JsonSchemaStepConfigForm
from validibot.workflows.forms import XmlSchemaStepConfigForm
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep
from validibot.workflows.models import WorkflowStepResource

logger = logging.getLogger(__name__)


def user_has_executor_role(user: User, workflow: Workflow) -> bool:
    """
    Return True when the user has EXECUTOR access to the workflow.
    """
    return user.has_perm(PermissionCode.WORKFLOW_LAUNCH.value, workflow)


def user_has_workflow_manager_role(user: User, workflow: Workflow) -> bool:
    """
    Return True when the user can manage the workflow (author/admin/owner).
    """

    if not getattr(user, "is_authenticated", False):
        return False
    return user.has_perm(PermissionCode.WORKFLOW_EDIT.value, workflow)


def resolve_project(
    workflow: Workflow,
    request: Request | HttpRequest,
) -> Project | None:
    """
    Derive the project for a workflow launch request, honoring ?project= overrides.
    """
    project_id = None
    if hasattr(request, "query_params"):
        project_id = request.query_params.get("project")
    if not project_id:
        project_id = request.GET.get("project")
    if project_id:
        try:
            return Project.objects.get(pk=project_id, org=workflow.org)
        except Project.DoesNotExist as exc:  # pragma: no cover
            raise Http404 from exc
    return workflow.project


def file_type_label(value: str) -> str:
    try:
        return SubmissionFileType(value).label
    except Exception:
        return value


def describe_workflow_file_type_violation(
    workflow: Workflow,
    file_type: str,
) -> str | None:
    """
    Describe why the given workflow cannot accept submissions of the given file type.
    """

    if not file_type:
        return _("Select a file type before launching the workflow.")
    if not workflow.supports_file_type(file_type):
        allowed = workflow.allowed_file_type_labels()
        allowed_display = ", ".join(allowed) if allowed else _("no file types")
        return _("This workflow accepts %(allowed)s submissions.") % {
            "allowed": allowed_display
        }
    blocking_step = workflow.first_incompatible_step(file_type)
    if blocking_step:
        validator_name = getattr(blocking_step.validator, "name", "")
        label = file_type_label(file_type)
        if validator_name:
            return _(
                "Step %(step)s (%(validator)s) does not support %(file_type)s files."
            ) % {
                "step": blocking_step.step_number_display,
                "validator": validator_name,
                "file_type": label,
            }
        return _("Step %(step)s does not support %(file_type)s files.") % {
            "step": blocking_step.step_number_display,
            "file_type": label,
        }
    return None


def resolve_submission_file_type(
    *,
    requested: str,
    filename: str,
    inline_text: str | None = None,
) -> str:
    """
    Determine the submission file type based on user request and file detection.
    """
    detected = detect_file_type(
        filename=filename or None,
        text=inline_text if inline_text else None,
    )
    if detected and detected != SubmissionFileType.UNKNOWN:
        return detected
    return requested


def build_public_info_url(request, workflow: Workflow) -> str | None:
    """
    Build the public info URL for the given workflow, if public info is enabled.
    """
    if not workflow.make_info_page_public:
        return None
    return request.build_absolute_uri(
        reverse(
            "workflow_public_info",
            kwargs={"workflow_uuid": workflow.uuid},
        ),
    )


def public_info_card_context(
    request,
    workflow: Workflow,
    *,
    can_manage: bool,
) -> dict[str, object]:
    """
    Build context for the public info card for the given workflow.
    """
    return {
        "workflow": workflow,
        "public_info_url": build_public_info_url(request, workflow),
        "can_manage_public_info": can_manage,
    }


def resequence_workflow_steps(workflow: Workflow) -> None:
    ordered = list(workflow.steps.all().order_by("order", "pk"))
    changed = False
    for index, step in enumerate(ordered, start=1):
        desired = index * 10
        if step.order != desired:
            step.order = desired
            changed = True
    if changed:
        WorkflowStep.objects.bulk_update(ordered, ["order"])


def ensure_ruleset(
    *,
    workflow: Workflow,
    step: WorkflowStep | None,
    ruleset_type: str,
) -> Ruleset:
    """
    Ensure a ruleset exists for the given workflow step and type.
    """

    if step and step.ruleset and step.ruleset.ruleset_type == ruleset_type:
        ruleset = step.ruleset
    else:
        ruleset = Ruleset(org=workflow.org, user=workflow.user)
    ruleset.org = workflow.org
    ruleset.user = workflow.user
    ruleset.ruleset_type = ruleset_type
    ruleset.name = ruleset.name or f"ruleset-{uuid4().hex[:8]}"
    ruleset.version = ruleset.version or "1"
    return ruleset


def ensure_advanced_ruleset(
    workflow: Workflow,
    step: WorkflowStep | None,
    validator: Validator,
) -> Ruleset:
    """Guarantee a ruleset exists for validators requiring assertions."""
    ruleset = getattr(step, "ruleset", None)
    if ruleset is None:
        base_name = f"{validator.slug}-ruleset"
        ruleset_name = unique_ruleset_name(
            org=workflow.org,
            ruleset_type=validator.validation_type,
            base_name=base_name,
            version="1",
        )
        ruleset = Ruleset(
            org=workflow.org,
            user=workflow.user,
            name=ruleset_name,
            ruleset_type=validator.validation_type,
            version="1",
        )
        ruleset.save()
        if step:
            step.ruleset = ruleset
            if step.pk:
                step.save(update_fields=["ruleset", "modified"])
    return ruleset


def unique_ruleset_name(
    *,
    org: Organization,
    ruleset_type: str,
    base_name: str,
    version: str,
) -> str:
    name = base_name
    suffix = 2
    while Ruleset.objects.filter(
        org=org,
        ruleset_type=ruleset_type,
        name=name,
        version=version,
    ).exists():
        truncated_base = base_name[: max(0, 240)]
        name = f"{truncated_base}-{suffix}"
        suffix += 1
    return name


def build_json_schema_config(
    workflow: Workflow,
    form: JsonSchemaStepConfigForm,
    step: WorkflowStep | None,
) -> tuple[dict[str, Any], Ruleset | None]:
    source = form.cleaned_data.get("schema_source")
    text = (form.cleaned_data.get("schema_text") or "").strip()
    uploaded = form.cleaned_data.get("schema_file")
    schema_type = form.cleaned_data.get("schema_type")

    if schema_type not in JSONSchemaVersion.values:
        raise ValidationError(_("Select a valid JSON Schema draft."))

    if source == "keep" and step and step.ruleset_id:
        preview = step.config.get("schema_text_preview", "")
        ruleset = step.ruleset
        metadata = dict(ruleset.metadata or {})
        metadata["schema_type"] = schema_type
        metadata.pop("schema", None)
        ruleset.metadata = metadata
        ruleset.full_clean()
        ruleset.save(update_fields=["metadata"])
        config = {
            "schema_source": "keep",
            "schema_text_preview": preview,
            "schema_type": schema_type,
            "schema_type_label": str(JSONSchemaVersion(schema_type).label),
        }
        return config, ruleset

    ruleset = ensure_ruleset(
        workflow=workflow,
        step=step,
        ruleset_type=RulesetType.JSON_SCHEMA,
    )

    schema_payload: str | None = None

    if source == "text":
        ruleset.rules_text = text
        if ruleset.rules_file:
            ruleset.rules_file.delete(save=False)
        ruleset.rules_file = None
        schema_payload = text
        preview = text[:1200]
    else:
        if uploaded is None:
            raise ValidationError(_("Upload a JSON schema file."))
        uploaded.seek(0)
        raw_bytes = uploaded.read()
        uploaded.seek(0)
        if ruleset.rules_file:
            ruleset.rules_file.delete(save=False)
        ruleset.rules_file.save(uploaded.name, uploaded, save=False)
        ruleset.rules_text = ""
        schema_payload = (
            raw_bytes.decode("utf-8", errors="replace")
            if isinstance(raw_bytes, bytes)
            else str(raw_bytes or "")
        )
        preview = schema_payload[:1200]

    metadata = dict(ruleset.metadata or {})
    metadata["schema_type"] = schema_type
    metadata.pop("schema", None)
    ruleset.metadata = metadata
    ruleset.full_clean()
    ruleset.save()

    config = {
        "schema_source": source,
        "schema_text_preview": preview,
        "schema_type": schema_type,
        "schema_type_label": str(JSONSchemaVersion(schema_type).label),
    }
    return config, ruleset


def build_xml_schema_config(
    workflow: Workflow,
    form: XmlSchemaStepConfigForm,
    step: WorkflowStep | None,
) -> tuple[dict[str, Any], Ruleset | None]:
    source = form.cleaned_data.get("schema_source")
    text = (form.cleaned_data.get("schema_text") or "").strip()
    uploaded = form.cleaned_data.get("schema_file")
    schema_type = form.cleaned_data.get("schema_type")

    if schema_type not in XMLSchemaType.values:
        raise ValidationError(_("Select a valid XML schema type."))

    if source == "keep" and step and step.ruleset_id:
        preview = step.config.get("schema_text_preview", "")
        ruleset = step.ruleset
        metadata = dict(ruleset.metadata or {})
        metadata["schema_type"] = schema_type
        metadata.pop("schema", None)
        ruleset.metadata = metadata
        ruleset.full_clean()
        ruleset.save(update_fields=["metadata"])
        config = {
            "schema_source": "keep",
            "schema_type": schema_type,
            "schema_text_preview": preview,
            "schema_type_label": str(XMLSchemaType(schema_type).label),
        }
        return config, ruleset

    ruleset = ensure_ruleset(
        workflow=workflow,
        step=step,
        ruleset_type=RulesetType.XML_SCHEMA,
    )

    if source == "text":
        ruleset.rules_text = text
        if ruleset.rules_file:
            ruleset.rules_file.delete(save=False)
        ruleset.rules_file = None
        preview = text[:1200]
    else:
        if uploaded is None:
            raise ValidationError(_("Upload an XML schema file."))
        uploaded.seek(0)
        raw_bytes = uploaded.read()
        uploaded.seek(0)
        if ruleset.rules_file:
            ruleset.rules_file.delete(save=False)
        ruleset.rules_file.save(uploaded.name, uploaded, save=False)
        ruleset.rules_text = ""
        schema_payload = (
            raw_bytes.decode("utf-8", errors="replace")
            if isinstance(raw_bytes, bytes)
            else str(raw_bytes or "")
        )
        preview = schema_payload[:1200]

    metadata = dict(ruleset.metadata or {})
    metadata["schema_type"] = schema_type
    metadata.pop("schema", None)
    ruleset.metadata = metadata
    ruleset.full_clean()
    ruleset.save()

    config = {
        "schema_source": source,
        "schema_type": schema_type,
        "schema_text_preview": preview,
        "schema_type_label": str(XMLSchemaType(schema_type).label),
    }
    return config, ruleset


def _validate_and_scan_template(
    template_file,
    *,
    case_sensitive: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate an IDF template and return ``(variable_dicts, warnings)``.

    Reads the file, runs the validation pipeline, and converts the scan
    result into a list of variable dicts ready for ``step.config``.

    Raises:
        ValidationError: If the template fails validation checks.
    """
    from validibot.validations.utils.idf_template import validate_idf_template

    content = template_file.read()
    result = validate_idf_template(
        filename=template_file.name,
        content=content,
        case_sensitive=case_sensitive,
    )

    if result.errors:
        raise ValidationError(result.errors)

    template_vars = [
        {
            "name": var_ctx.name,
            "description": var_ctx.label,
            "default": "",
            "units": var_ctx.units,
            "variable_type": "text",
            "min_value": None,
            "min_exclusive": False,
            "max_value": None,
            "max_exclusive": False,
            "choices": [],
        }
        for var_ctx in result.scan_result.variables
    ]
    return template_vars, result.warnings


def build_energyplus_config(
    form: EnergyPlusStepConfigForm,
    step: WorkflowStep | None = None,
) -> dict[str, Any]:
    """Build the JSON config dict for an EnergyPlus step.

    The ``validation_mode`` field determines which config keys are
    populated:

    - **direct**: ``idf_checks`` and ``run_simulation`` are stored.
      Template metadata is cleared.
    - **template**: Template variables, case sensitivity, and display
      signals are stored.  IDF-check and simulation flags are omitted
      (the template pipeline always runs the simulation).

    Resource file references (weather files, templates) are stored
    relationally via ``WorkflowStepResource`` and are synced separately
    by ``_sync_energyplus_resources()`` after the step is saved.

    Template handling:

    - **Template upload**: Validates the IDF, scans for ``$VARIABLE_NAME``
      placeholders, and populates ``template_variables`` in the config.
      Raises ``ValidationError`` if the file fails validation.
    - **Template removal** (switching to direct mode or explicit remove):
      Clears ``template_variables`` and resets ``case_sensitive`` to True.
    - **No change**: Preserves existing ``template_variables`` from the
      step's current config (if any).

    The template *file* itself is persisted by
    ``_sync_energyplus_resources()`` after ``step.save()``.
    """
    validation_mode = form.cleaned_data.get(
        "validation_mode",
        EnergyPlusStepConfigForm.VALIDATION_MODE_DIRECT,
    )

    config: dict[str, Any] = {
        "validation_mode": validation_mode,
        "show_energyplus_warnings": form.cleaned_data.get(
            "show_energyplus_warnings",
            True,
        ),
    }

    if validation_mode == EnergyPlusStepConfigForm.VALIDATION_MODE_DIRECT:
        # Direct IDF mode — store IDF check/simulation settings,
        # clear any template metadata.
        config["idf_checks"] = form.cleaned_data.get("idf_checks", [])
        config["run_simulation"] = form.cleaned_data.get("run_simulation", False)
        config["template_variables"] = []
        config["case_sensitive"] = True
        config["display_signals"] = []
        # Signal _sync_energyplus_resources to remove the template file
        # if one exists from a previous template-mode configuration.
        form.cleaned_data["remove_template"] = True
        return config

    # ── Template mode ─────────────────────────────────────────────
    config["idf_checks"] = []
    config["run_simulation"] = True

    remove_template = form.cleaned_data.get("remove_template", False)
    template_file = form.cleaned_data.get("template_file")

    if remove_template:
        # Author clicked "Remove template" — clear all template metadata.
        config["template_variables"] = []
        config["case_sensitive"] = True
        config["display_signals"] = []
    elif template_file:
        # New template uploaded — validate, scan, and populate config.
        template_vars, template_warnings = _validate_and_scan_template(
            template_file,
            case_sensitive=form.cleaned_data.get("case_sensitive", True),
        )

        config["template_variables"] = template_vars
        config["case_sensitive"] = form.cleaned_data.get("case_sensitive", True)
        config["display_signals"] = []

        # Attach warnings to the form so the view can display them.
        form.template_warnings = template_warnings
    elif step:
        # No upload, no removal — preserve existing template variables
        # as-is.  Variable annotation editing happens in the dedicated
        # template variables card on the step detail page (not here).
        existing_config = step.config or {}
        existing_vars = existing_config.get("template_variables", [])
        if existing_vars:
            config["template_variables"] = existing_vars
            config["case_sensitive"] = form.cleaned_data.get("case_sensitive", True)
            config["display_signals"] = existing_config.get("display_signals", [])

    return config


def _parse_optional_float(value: str) -> float | None:
    """Convert a string to float, returning None for empty or invalid values.

    Used by ``build_energyplus_config()`` to parse min/max values from
    the template variable editor form fields.
    """
    if not value or not value.strip():
        return None
    try:
        return float(value.strip())
    except (ValueError, TypeError):
        return None


def _parse_choices(value: str) -> list[str]:
    """Split a newline-separated string into a list of non-empty choices.

    Used by ``build_energyplus_config()`` to parse the "Allowed values"
    textarea from the template variable editor.
    """
    if not value:
        return []
    return [line.strip() for line in value.splitlines() if line.strip()]


def merge_template_variable_annotations(
    existing_vars: list[dict[str, Any]],
    form_data: dict[str, Any],
) -> list[dict[str, Any]]:
    """Merge author annotations from the template variable editor form.

    Takes the existing template variable dicts (from ``step.config``)
    and merges in the updated annotations from the form's cleaned_data.
    Variable **names** are immutable (set during IDF scan); all other
    fields (description, default, units, type, constraints, choices)
    can be updated by the author.

    Called by the ``WorkflowStepTemplateVariablesView`` when saving
    annotations from the step detail page's template variables card.

    Args:
        existing_vars: List of template variable dicts from step config.
        form_data: The form's ``cleaned_data`` dict containing
            ``tplvar_{i}_{field_name}`` keys.

    Returns:
        New list of template variable dicts with merged annotations.
    """
    merged = []
    for i, var in enumerate(existing_vars):
        prefix = f"tplvar_{i}"
        merged.append(
            {
                "name": var["name"],
                "description": form_data.get(
                    f"{prefix}_description",
                    var.get("description", ""),
                ),
                "default": form_data.get(
                    f"{prefix}_default",
                    var.get("default", ""),
                ),
                "units": form_data.get(
                    f"{prefix}_units",
                    var.get("units", ""),
                ),
                "variable_type": form_data.get(
                    f"{prefix}_variable_type",
                    var.get("variable_type", "text"),
                ),
                "min_value": _parse_optional_float(
                    form_data.get(f"{prefix}_min_value", ""),
                ),
                "min_exclusive": form_data.get(
                    f"{prefix}_min_exclusive",
                    False,
                ),
                "max_value": _parse_optional_float(
                    form_data.get(f"{prefix}_max_value", ""),
                ),
                "max_exclusive": form_data.get(
                    f"{prefix}_max_exclusive",
                    False,
                ),
                "choices": _parse_choices(
                    form_data.get(f"{prefix}_choices", ""),
                ),
            }
        )
    return merged


def step_has_template_variables(step: WorkflowStep) -> bool:
    """Condition function for the template variables step editor card.

    Returns True when the step has template variables configured,
    indicating the template variables card should be rendered.
    """
    return bool((step.config or {}).get("template_variables"))


def build_unified_signals(
    catalog_display: Any | None,
    step: WorkflowStep,
) -> dict[str, Any]:
    """Build unified input/output signal lists for the step detail card.

    Merges catalog entries (from the validator's config) with template
    variables (from the step's config) into a single list per stage.
    This gives the workflow author one consistent view of "what goes in,
    what comes out" regardless of whether signals come from the catalog
    or from an uploaded template.

    See ADR-2026-03-10: Unified Input/Output Signals UI.

    Returns a dict with keys:
        input_signals: List of dicts, each with slug, label, source,
            required, and either catalog_entry or variable metadata.
        output_signals: List of dicts, each with slug, label,
            show_to_user flag, and catalog_entry.
        has_inputs: Whether any input signals exist.
        has_outputs: Whether any output signals exist.
    """
    step_config = step.config or {}
    display_signals = step_config.get("display_signals", [])
    template_vars = step_config.get("template_variables", [])

    # -- Input signals --
    input_signals: list[dict[str, Any]] = []

    # When template variables exist, they *are* the inputs — the submitter
    # provides parameter values, not a full file.  Catalog INPUT entries
    # (e.g. submission metadata fields) are irrelevant in template mode
    # because the submission shape is entirely defined by the template.
    if catalog_display and not template_vars:
        for entry in catalog_display.inputs:
            default_val = (
                str(entry.default_value) if entry.default_value is not None else ""
            )
            input_signals.append(
                {
                    "slug": entry.slug,
                    "label": entry.label or entry.slug,
                    "source": "catalog",
                    "required": entry.is_required,
                    "default_value": default_val,
                    "catalog_entry": entry,
                    "variable_index": None,
                    "variable": None,
                },
            )
        for entry in catalog_display.input_derivations:
            input_signals.append(
                {
                    "slug": entry.slug,
                    "label": entry.label or entry.slug,
                    "source": "catalog",
                    "required": False,
                    "default_value": "",
                    "catalog_entry": entry,
                    "variable_index": None,
                    "variable": None,
                },
            )
    for i, var in enumerate(template_vars):
        default_val = var.get("default", "")
        input_signals.append(
            {
                "slug": var.get("name", ""),
                "label": var.get("description") or var.get("name", ""),
                "source": "template",
                "required": not bool(default_val),
                "default_value": default_val,
                "catalog_entry": None,
                "variable_index": i,
                "variable": var,
            },
        )

    # -- Output signals --
    output_signals: list[dict[str, Any]] = []

    if catalog_display:
        for entry in catalog_display.outputs:
            show = _is_signal_shown(entry.slug, display_signals)
            output_signals.append(
                {
                    "slug": entry.slug,
                    "label": entry.label or entry.slug,
                    "show_to_user": show,
                    "catalog_entry": entry,
                },
            )
        for entry in catalog_display.output_derivations:
            show = _is_signal_shown(entry.slug, display_signals)
            output_signals.append(
                {
                    "slug": entry.slug,
                    "label": entry.label or entry.slug,
                    "show_to_user": show,
                    "catalog_entry": entry,
                },
            )

    return {
        "input_signals": input_signals,
        "output_signals": output_signals,
        "has_inputs": bool(input_signals),
        "has_outputs": bool(output_signals),
    }


def _is_signal_shown(slug: str, display_signals: list[str]) -> bool:
    """Determine whether an output signal should show a green check.

    Empty display_signals means "show all" (backward-compatible default).
    """
    if not display_signals:
        return True
    return slug in display_signals


def _sync_energyplus_resources(
    step: WorkflowStep,
    form: EnergyPlusStepConfigForm,
) -> None:
    """Sync the relational ``WorkflowStepResource`` rows for an EnergyPlus step.

    Called *after* ``step.save()`` so the step has a PK. Replaces the old
    approach of storing UUID strings in ``config["resource_file_ids"]``.

    Handles two resource roles:

    - **WEATHER_FILE** — catalog reference to a shared ``ValidatorResourceFile``.
    - **MODEL_TEMPLATE** — step-owned file uploaded for parameterized templates
      (Phase 2).  The template file is stored directly on the
      ``WorkflowStepResource`` via ``step_resource_file``.
    """
    from validibot.validations.constants import ENERGYPLUS_MODEL_TEMPLATE
    from validibot.validations.models import ValidatorResourceFile

    # ── Weather file ────────────────────────────────────────────────
    weather_file_id = form.cleaned_data.get("weather_file", "")

    # Remove existing weather file resources for this step
    step.step_resources.filter(role=WorkflowStepResource.WEATHER_FILE).delete()

    # Create new one if a weather file was selected
    if weather_file_id:
        try:
            vrf = ValidatorResourceFile.objects.get(pk=weather_file_id)
            WorkflowStepResource.objects.create(
                step=step,
                role=WorkflowStepResource.WEATHER_FILE,
                validator_resource_file=vrf,
            )
        except ValidatorResourceFile.DoesNotExist:
            logger.warning(
                "Weather file UUID %s not found when saving step %s.",
                weather_file_id,
                step.pk,
            )

    # ── Model template (Phase 2) ──────────────────────────────────
    remove_template = form.cleaned_data.get("remove_template", False)
    template_file = form.cleaned_data.get("template_file")

    if remove_template:
        # Author chose to remove the template — delete the resource row
        # (and the step-owned file via Django storage cleanup).
        step.step_resources.filter(
            role=WorkflowStepResource.MODEL_TEMPLATE,
        ).delete()
    elif template_file:
        # New template uploaded — replace any existing template resource.
        step.step_resources.filter(
            role=WorkflowStepResource.MODEL_TEMPLATE,
        ).delete()

        # Reset the file pointer — build_energyplus_config() already
        # called .read() for validation.
        template_file.seek(0)

        WorkflowStepResource.objects.create(
            step=step,
            role=WorkflowStepResource.MODEL_TEMPLATE,
            step_resource_file=template_file,
            filename=template_file.name,
            resource_type=ENERGYPLUS_MODEL_TEMPLATE,
        )


def build_ai_config(form: AiAssistStepConfigForm) -> dict[str, Any]:
    selectors = form.cleaned_data.get("selectors", [])
    policy_rules = form.cleaned_data.get("policy_rules", [])
    return {
        "template": form.cleaned_data.get("template"),
        "mode": form.cleaned_data.get("mode"),
        "cost_cap_cents": form.cleaned_data.get("cost_cap_cents"),
        "selectors": selectors,
        "policy_rules": [asdict(rule) for rule in policy_rules],
    }


def save_workflow_step(
    workflow: Workflow,
    validator: Validator,
    form: forms.Form,
    *,
    step: WorkflowStep | None = None,
) -> WorkflowStep:
    """
    Persist a workflow step using the supplied form data and validator.
    """
    is_new = step is None
    step = step or WorkflowStep(workflow=workflow)
    step.validator = validator
    step.action = None
    step.name = form.cleaned_data.get("name", "").strip() or validator.name
    step.description = (form.cleaned_data.get("description") or "").strip()
    step.notes = (form.cleaned_data.get("notes") or "").strip()
    if "display_schema" in form.cleaned_data:
        step.display_schema = form.cleaned_data.get("display_schema", False)
    if "show_success_messages" in form.cleaned_data:
        step.show_success_messages = form.cleaned_data.get(
            "show_success_messages",
            False,
        )

    config: dict[str, Any]
    ruleset: Ruleset | None = None
    vtype = validator.validation_type

    if vtype == ValidationType.JSON_SCHEMA:
        config, ruleset = build_json_schema_config(workflow, form, step)
    elif vtype == ValidationType.XML_SCHEMA:
        config, ruleset = build_xml_schema_config(workflow, form, step)
    elif vtype == ValidationType.ENERGYPLUS:
        config = build_energyplus_config(form, step)
        # File type enforcement: parameterized templates require JSON-only
        # submissions (the submitter sends variable values as a flat JSON
        # object, not an IDF or epJSON file).  Allowing other file types
        # alongside JSON would let users upload IDF files that the launcher
        # would attempt to parse as JSON parameters — causing a confusing
        # error downstream instead of a clear rejection at upload time.
        if config.get("template_variables"):
            allowed = [ft.lower() for ft in (workflow.allowed_file_types or [])]
            if allowed != [SubmissionFileType.JSON.lower()]:
                raise ValidationError(
                    _(
                        "This step uses a parameterized template, which "
                        "requires JSON-only submissions. Please set the "
                        "workflow's allowed file types to JSON only before "
                        "activating a template."
                    )
                )
    elif vtype == ValidationType.FMU:
        config = {}
    elif vtype == ValidationType.AI_ASSIST:
        config = build_ai_config(form)
    else:
        config = {}

    if ruleset is not None:
        step.ruleset = ruleset
    elif validator and validator.supports_assertions:
        step.ruleset = ensure_advanced_ruleset(workflow, step, validator)
    elif vtype not in (ValidationType.JSON_SCHEMA, ValidationType.XML_SCHEMA):
        step.ruleset = None

    step.config = config

    if is_new:
        max_order = workflow.steps.aggregate(max_order=models.Max("order"))["max_order"]
        step.order = (max_order or 0) + 10

    step.save()

    # Sync relational resource bindings (weather files, templates) after
    # step.save() gives us a PK for new steps.
    if vtype == ValidationType.ENERGYPLUS:
        _sync_energyplus_resources(step, form)

    return step


def save_workflow_action_step(
    workflow: Workflow,
    definition: ActionDefinition,
    form: forms.Form,
    *,
    step: WorkflowStep | None = None,
) -> WorkflowStep:
    """Persist a workflow step that references an action definition."""

    is_new = step is None
    step = step or WorkflowStep(workflow=workflow)
    action = getattr(step, "action", None)

    if not hasattr(form, "save_action"):
        raise ValueError("Action forms must implement save_action().")

    action = form.save_action(
        definition,
        current_action=action,
    )

    step.validator = None
    step.ruleset = None
    step.action = action
    step.name = action.name
    step.description = action.description
    step.notes = (form.cleaned_data.get("notes") or "").strip()
    step.display_schema = False
    step.show_success_messages = form.cleaned_data.get("show_success_messages", False)
    summary = {}
    if hasattr(form, "build_step_summary"):
        summary = form.build_step_summary(action) or {}
    step.config = summary

    if is_new:
        max_order = workflow.steps.aggregate(max_order=models.Max("order"))["max_order"]
        step.order = (max_order or 0) + 10

    step.save()
    return step
