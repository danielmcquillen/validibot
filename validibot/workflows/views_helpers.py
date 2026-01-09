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
from validibot.validations.constants import ADVANCED_VALIDATION_TYPES
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


def build_energyplus_config(form: EnergyPlusStepConfigForm) -> dict[str, Any]:
    return {
        "weather_file": form.cleaned_data.get("weather_file", ""),
        "idf_checks": form.cleaned_data.get("idf_checks", []),
        "run_simulation": form.cleaned_data.get("run_simulation", False),
        "notes": form.cleaned_data.get("energyplus_notes", ""),
    }


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

    config: dict[str, Any]
    ruleset: Ruleset | None = None
    vtype = validator.validation_type

    if vtype == ValidationType.JSON_SCHEMA:
        config, ruleset = build_json_schema_config(workflow, form, step)
    elif vtype == ValidationType.XML_SCHEMA:
        config, ruleset = build_xml_schema_config(workflow, form, step)
    elif vtype == ValidationType.ENERGYPLUS:
        config = build_energyplus_config(form)
    elif vtype == ValidationType.FMI:
        config = {}
    elif vtype == ValidationType.AI_ASSIST:
        config = build_ai_config(form)
    else:
        config = {}

    if ruleset is not None:
        step.ruleset = ruleset
    elif vtype in ADVANCED_VALIDATION_TYPES:
        step.ruleset = ensure_advanced_ruleset(workflow, step, validator)
    elif vtype not in (ValidationType.JSON_SCHEMA, ValidationType.XML_SCHEMA):
        step.ruleset = None

    step.config = config

    if is_new:
        max_order = workflow.steps.aggregate(max_order=models.Max("order"))["max_order"]
        step.order = (max_order or 0) + 10

    step.save()
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
    summary = {}
    if hasattr(form, "build_step_summary"):
        summary = form.build_step_summary(action) or {}
    step.config = summary

    if is_new:
        max_order = workflow.steps.aggregate(max_order=models.Max("order"))["max_order"]
        step.order = (max_order or 0) + 10

    step.save()
    return step
