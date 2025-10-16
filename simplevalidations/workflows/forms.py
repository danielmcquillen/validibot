from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from dataclasses import dataclass
from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Field
from crispy_forms.layout import Layout
from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from simplevalidations.projects.models import Project
from simplevalidations.users.models import User
from simplevalidations.validations.constants import RulesetType
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.constants import XMLSchemaType
from simplevalidations.validations.models import Validator
from simplevalidations.workflows.constants import SUPPORTED_CONTENT_TYPES
from simplevalidations.workflows.models import Workflow
from simplevalidations.workflows.models import WorkflowPublicInfo

AI_TEMPLATES = (
    ("ai_critic", _("AI Critic")),
    ("policy_check", _("Policy Check")),
)

AI_MODES = (
    ("ADVISORY", _("Advisory (warnings only)")),
    ("BLOCKING", _("Blocking (fail on violations)")),
)

ENERGYPLUS_IDF_CHECK_CHOICES = (
    ("duplicate-names", _("Detect duplicate object names")),
    ("hvac-sizing", _("Ensure HVAC autosizing is enabled")),
    ("schedule-coverage", _("Check schedules cover 7 days")),
)

ENERGYPLUS_SIMULATION_CHECK_CHOICES = (
    ("eui-range", _("Flag if Energy Use Intensity is outside range")),
    ("peak-load", _("Check peak heating/cooling load")),
)


@dataclass(slots=True)
class ParsedPolicyRule:
    identifier: str
    path: str
    operator: str
    value: Any
    value_b: Any | None
    message: str


class RuleParseError(Exception):
    """Raised when a policy rule cannot be parsed."""


def _parse_list_literal(raw: str) -> list[str]:
    text = raw.strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuleParseError(str(exc)) from exc
        if not isinstance(parsed, list):
            raise RuleParseError(_("Expected list for 'in' operator."))
        return [str(item) for item in parsed]
    return [part.strip() for part in text.split(",") if part.strip()]


def parse_policy_rules(raw_text: str) -> list[ParsedPolicyRule]:
    rules: list[ParsedPolicyRule] = []
    if not raw_text:
        return rules

    for line in raw_text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        message = ""
        if "|" in stripped:
            stripped, message = [part.strip() for part in stripped.split("|", 1)]
        segments = stripped.split()
        if len(segments) < 2:
            raise RuleParseError(
                _("Rule lines must include at least a path and operator."),
            )
        path = segments[0]
        operator = segments[1].lower()
        value: Any = None
        value_b: Any | None = None

        match operator:
            case "between":
                if len(segments) < 4:
                    raise RuleParseError(
                        _("'between' rules require two numeric bounds."),
                    )
                value = segments[2]
                value_b = segments[3]
            case "in" | "not_in":
                if len(segments) < 3:
                    raise RuleParseError(
                        _("'%s' rules require a list of options.") % operator,
                    )
                value = _parse_list_literal(" ".join(segments[2:]))
            case "nonempty":
                value = None
            case _:
                if len(segments) < 3:
                    raise RuleParseError(
                        _("Operator '%(op)s' requires a comparison value."),
                    ) % {"op": operator}
                value = segments[2]

        identifier = f"rule-{uuid.uuid4().hex[:8]}"
        rules.append(
            ParsedPolicyRule(
                identifier=identifier,
                path=path,
                operator=operator,
                value=value,
                value_b=value_b,
                message=message,
            ),
        )
    return rules


class WorkflowForm(forms.ModelForm):
    class Meta:
        model = Workflow
        fields = [
            "name",
            "slug",
            "project",
            "featured_image",
            "version",
            "is_active",
            "make_info_public",
        ]
        help_texts = {
            "version": _("Optional label to help you track iterations."),
            "is_active": _(
                "Disable a workflow to pause new validation runs without removing it.",
            ),
        }

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Field("name", placeholder=_("Name your workflow"), autofocus=True),
            Field("slug", placeholder=""),
            Field("project"),
            Field("featured_image"),
            Field("version", placeholder="e.g. 1.0"),
            Field("is_active"),
            Field("make_info_public"),
        )
        self._configure_project_field()
        self.fields["is_active"].label = _("Workflow active")
        self.fields["is_active"].help_text = _(
            "When unchecked, teammates can still view the workflow but cannot "
            "launch runs until you reactivate it.",
        )
        self.fields["make_info_public"].label = _("Make info public")
        self.fields["make_info_public"].help_text = _(
            "Allows non-logged in users to see details of the workflow validation.",
        )
        self.fields["featured_image"].widget = forms.ClearableFileInput()
        self.fields["featured_image"].widget.attrs.update({"class": "form-control"})
        self.fields["featured_image"].label = _("Featured image")
        self.fields["featured_image"].help_text = _(
            "Optional image shown on the workflow info page.",
        )

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise ValidationError(_("Name is required."))
        return name

    def _configure_project_field(self):
        project_field = self.fields.get("project")
        if project_field is None:
            return

        project_field.required = True
        project_field.empty_label = _("Select a project")
        project_field.queryset = Project.objects.none()

        if not self.user or not getattr(self.user, "is_authenticated", False):
            return

        org = self.user.get_current_org()
        if not org:
            return

        projects = Project.objects.filter(org=org).order_by("name")
        project_field.queryset = projects

        if self.instance and self.instance.pk and self.instance.project_id:
            return

        if not self.initial.get("project"):
            default_project = projects.filter(is_default=True).first()
            if default_project:
                project_field.initial = default_project.pk


class WorkflowLaunchForm(forms.Form):
    filename = forms.CharField(
        label=_("Filename"),
        required=False,
        help_text=_("Optional override used for inline submissions."),
    )
    content_type = forms.ChoiceField(
        label=_("Content type"),
        choices=[],
    )
    payload = forms.CharField(
        label=_("Inline content"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 10,
                "placeholder": _('{ "example": "value" }'),
            },
        ),
        help_text=_(
            "Paste JSON, XML, or text. Leave blank when uploading a file.",
        ),
    )
    attachment = forms.FileField(
        label=_("Attachment"),
        required=False,
        help_text=_("Upload a file instead of pasting inline content."),
    )
    metadata = forms.CharField(
        label=_("Metadata (JSON)"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 4,
                "placeholder": _('{"source": "ui"}'),
            },
        ),
        help_text=_("Optional JSON payload stored with the submission."),
    )

    def __init__(self, *args, workflow: Workflow, user: User | None = None, **kwargs):
        self.workflow = workflow
        self.user = user
        super().__init__(*args, **kwargs)
        self._apply_bootstrap_styles()
        self._configure_content_type_field()

    def _configure_content_type_field(self) -> None:
        content_type_field = self.fields["content_type"]
        choices: list[tuple[str, str]] = []
        for content_type, file_type in SUPPORTED_CONTENT_TYPES.items():
            label = f"{file_type.label} ({content_type})"
            choices.append((content_type, label))
        content_type_field.choices = choices
        if not content_type_field.initial:
            content_type_field.initial = "application/json"

    def _apply_bootstrap_styles(self) -> None:
        for field in self.fields.values():
            widget = field.widget
            base_class = widget.attrs.get("class", "")
            match widget.__class__.__name__:
                case "Select":
                    widget.attrs["class"] = f"{base_class} form-select".strip()
                case "Textarea":
                    widget.attrs["class"] = f"{base_class} form-control".strip()
                case "ClearableFileInput" | "FileInput":
                    widget.attrs["class"] = f"{base_class} form-control".strip()
                case _:
                    widget.attrs["class"] = f"{base_class} form-control".strip()

    def clean(self):
        cleaned = super().clean()
        payload = (cleaned.get("payload") or "").strip()
        attachment = cleaned.get("attachment")
        if payload and attachment:
            raise forms.ValidationError(
                _("Provide inline content or upload a file, not both."),
            )
        if not payload and not attachment:
            raise forms.ValidationError(
                _("Paste content or upload a file to launch the workflow."),
            )

        content_type = cleaned.get("content_type")
        if content_type not in SUPPORTED_CONTENT_TYPES:
            raise forms.ValidationError(
                _("Select a supported content type."),
            )
        cleaned["payload"] = payload

        metadata = cleaned.get("metadata")
        if metadata:
            try:
                cleaned["metadata"] = json.loads(metadata)
            except json.JSONDecodeError as exc:
                raise forms.ValidationError(
                    _("Metadata must be valid JSON."),
                ) from exc
        else:
            cleaned["metadata"] = {}

        return cleaned


class WorkflowStepTypeForm(forms.Form):
    validator = forms.ChoiceField(
        label=_("Validation type"),
        widget=forms.RadioSelect,
    )

    def __init__(self, *args, validators: list[Validator], **kwargs):
        super().__init__(*args, **kwargs)
        choices = [(str(validator.pk), f"{validator.name}") for validator in validators]
        self.fields["validator"].choices = choices
        self.validators = {str(validator.pk): validator for validator in validators}

    def get_validator(self) -> Validator:
        value = self.cleaned_data.get("validator")
        return self.validators[str(value)]


class BaseStepConfigForm(forms.Form):
    name = forms.CharField(
        label=_("Step name"),
        max_length=200,
        widget=forms.TextInput(
            attrs={"placeholder": _("Describe what this step checks")},
        ),
    )

    def initial_from_step(self, step) -> None:
        if step and step.name:
            self.fields["name"].initial = step.name


class JsonSchemaStepConfigForm(BaseStepConfigForm):
    schema_source = forms.ChoiceField(
        label=_("Schema source"),
        choices=(
            ("text", _("Paste schema")),
            ("upload", _("Upload file")),
        ),
        widget=forms.RadioSelect,
    )
    schema_text = forms.CharField(
        label=_("JSON Schema"),
        widget=forms.Textarea(attrs={"rows": 12, "spellcheck": "false"}),
        required=False,
    )
    schema_file = forms.FileField(
        label=_("Upload schema"),
        required=False,
    )

    def __init__(self, *args, step=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial_from_step(step)
        if step and step.ruleset_id:
            self.fields["schema_source"].choices += [
                ("keep", _("Keep existing schema"))
            ]
            self.fields["schema_source"].initial = step.config.get(
                "schema_source", "keep"
            )
            self.fields["schema_text"].initial = step.config.get(
                "schema_text_preview", ""
            )

    def clean(self):
        cleaned = super().clean()
        source = cleaned.get("schema_source")
        text = cleaned.get("schema_text", "").strip()
        file = cleaned.get("schema_file")
        if source == "text" and not text:
            self.add_error("schema_text", _("Provide JSON schema text."))
        if source == "upload" and not file:
            self.add_error("schema_file", _("Upload a JSON schema file."))
        return cleaned


class XmlSchemaStepConfigForm(BaseStepConfigForm):
    schema_type = forms.ChoiceField(
        label=_("Schema type"),
        choices=XMLSchemaType.choices,
    )
    schema_source = forms.ChoiceField(
        label=_("Schema source"),
        choices=(
            ("text", _("Paste schema")),
            ("upload", _("Upload file")),
        ),
        widget=forms.RadioSelect,
    )
    schema_text = forms.CharField(
        label=_("XML Schema"),
        widget=forms.Textarea(attrs={"rows": 12, "spellcheck": "false"}),
        required=False,
    )
    schema_file = forms.FileField(
        label=_("Upload schema"),
        required=False,
    )

    def __init__(self, *args, step=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial_from_step(step)
        if step and step.ruleset_id:
            self.fields["schema_source"].choices += [
                ("keep", _("Keep existing schema"))
            ]
            self.fields["schema_source"].initial = step.config.get(
                "schema_source", "keep"
            )
            self.fields["schema_text"].initial = step.config.get(
                "schema_text_preview", ""
            )
            self.fields["schema_type"].initial = step.config.get("schema_type")

    def clean(self):
        cleaned = super().clean()
        source = cleaned.get("schema_source")
        text = cleaned.get("schema_text", "").strip()
        file = cleaned.get("schema_file")
        if source == "text" and not text:
            self.add_error("schema_text", _("Provide XML schema text."))
        if source == "upload" and not file:
            self.add_error("schema_file", _("Upload an XML schema file."))
        return cleaned


class EnergyPlusStepConfigForm(BaseStepConfigForm):
    run_simulation = forms.BooleanField(
        label=_("Run EnergyPlus simulation"),
        required=False,
    )
    idf_checks = forms.MultipleChoiceField(
        label=_("Initial IDF checks"),
        required=False,
        choices=ENERGYPLUS_IDF_CHECK_CHOICES,
        widget=forms.CheckboxSelectMultiple,
    )
    simulation_checks = forms.MultipleChoiceField(
        label=_("Post-simulation checks"),
        required=False,
        choices=ENERGYPLUS_SIMULATION_CHECK_CHOICES,
        widget=forms.CheckboxSelectMultiple,
    )
    eui_min = forms.DecimalField(
        label=_("EUI minimum (kWh/m²)"),
        required=False,
        min_value=0,
        decimal_places=2,
    )
    eui_max = forms.DecimalField(
        label=_("EUI maximum (kWh/m²)"),
        required=False,
        min_value=0,
        decimal_places=2,
    )
    notes = forms.CharField(
        label=_("Notes"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, step=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial_from_step(step)
        if step:
            config = step.config or {}
            self.initial.update(
                {
                    "run_simulation": config.get("run_simulation", False),
                    "idf_checks": config.get("idf_checks", []),
                    "simulation_checks": config.get("simulation_checks", []),
                    "eui_min": config.get("eui_band", {}).get("min"),
                    "eui_max": config.get("eui_band", {}).get("max"),
                    "notes": config.get("notes", ""),
                }
            )
            for key, value in self.initial.items():
                if key in self.fields and value not in (None, ""):
                    self.fields[key].initial = value

    def clean(self):
        cleaned = super().clean()
        eui_min = cleaned.get("eui_min")
        eui_max = cleaned.get("eui_max")
        if eui_min is not None and eui_max is not None and eui_min > eui_max:
            raise ValidationError(_("EUI minimum cannot exceed maximum."))
        return cleaned


class AiAssistStepConfigForm(BaseStepConfigForm):
    template = forms.ChoiceField(
        label=_("AI template"),
        choices=AI_TEMPLATES,
        initial="ai_critic",
    )
    selectors = forms.CharField(
        label=_("Selectors"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": _("Optional JSONPath selectors (one per line)."),
                "spellcheck": "false",
            },
        ),
    )
    policy_rules = forms.CharField(
        label=_("Policy rules"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 4,
                "placeholder": _(
                    "$.zones[*].cooling_setpoint >= 18 | Cooling setpoint must be ≥18°C"
                ),
                "spellcheck": "false",
            },
        ),
    )
    cost_cap_cents = forms.IntegerField(
        label=_("Cost cap (cents)"),
        min_value=1,
        max_value=500,
        initial=10,
    )
    mode = forms.ChoiceField(
        label=_("Behaviour"),
        choices=AI_MODES,
        initial="ADVISORY",
    )

    def __init__(self, *args, step=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial_from_step(step)
        if step:
            config = step.config or {}
            self.fields["template"].initial = config.get("template", "ai_critic")
            self.fields["mode"].initial = config.get("mode", "ADVISORY")
            self.fields["cost_cap_cents"].initial = config.get("cost_cap_cents", 10)
            selectors = config.get("selectors") or []
            self.fields["selectors"].initial = "\n".join(selectors)
            policy_rules = config.get("policy_rules") or []
            lines: list[str] = []
            for rule in policy_rules:
                path = rule.get("path", "$")
                operator = rule.get("operator", "")
                value = rule.get("value")
                value_b = rule.get("value_b")
                message = rule.get("message", "")
                parts = [path, operator]
                if value is not None and operator not in {"nonempty"}:
                    if isinstance(value, list):
                        parts.append(json.dumps(value))
                    else:
                        parts.append(str(value))
                if value_b is not None:
                    parts.append(str(value_b))
                rule_line = " ".join(parts)
                if message:
                    rule_line = f"{rule_line} | {message}"
                lines.append(rule_line)
            self.fields["policy_rules"].initial = "\n".join(lines)

    def clean_selectors(self) -> list[str]:
        raw = self.cleaned_data.get("selectors", "")
        selectors = [line.strip() for line in raw.splitlines() if line.strip()]
        if len(selectors) > 10:
            raise ValidationError(_("Limit selectors to 10 paths."))
        return selectors

    def clean_policy_rules(self) -> list[ParsedPolicyRule]:
        raw = self.cleaned_data.get("policy_rules", "")
        try:
            return parse_policy_rules(raw)
        except RuleParseError as exc:
            raise ValidationError(str(exc)) from exc

    def clean(self):
        cleaned = super().clean()
        template = cleaned.get("template")
        rules = cleaned.get("policy_rules")
        if template == "policy_check" and not rules:
            raise ValidationError(_("Add at least one policy rule."))
        return cleaned


def get_config_form_class(validation_type: str) -> type[forms.Form]:
    mapping: dict[str, type[forms.Form]] = {
        ValidationType.JSON_SCHEMA: JsonSchemaStepConfigForm,
        ValidationType.XML_SCHEMA: XmlSchemaStepConfigForm,
        ValidationType.ENERGYPLUS: EnergyPlusStepConfigForm,
        ValidationType.AI_ASSIST: AiAssistStepConfigForm,
    }
    return mapping.get(validation_type, BaseStepConfigForm)


class WorkflowPublicInfoForm(forms.ModelForm):
    class Meta:
        model = WorkflowPublicInfo
        fields = ["title", "content_md"]
        widgets = {
            "title": forms.TextInput(
                attrs={"placeholder": _("Optional headline for the public page")},
            ),
            "content_md": forms.Textarea(
                attrs={
                    "rows": 12,
                    "placeholder": _(
                        "# Overview\nDescribe the workflow for public viewers...",
                    ),
                },
            ),
        }

    def __init__(self, *args, workflow: Workflow, **kwargs):
        self.workflow = workflow
        instance = kwargs.get("instance")
        if instance is None:
            instance = workflow.get_public_info
            kwargs["instance"] = instance
        super().__init__(*args, **kwargs)
        self.fields["title"].label = _("Public title")
        self.fields["content_md"].label = _("Public description (Markdown)")
        self.fields["title"].widget.attrs.setdefault("class", "form-control")
        self.fields["content_md"].widget.attrs.setdefault("class", "form-control")
        self.fields["title"].required = False
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Field("title"),
            Field("content_md"),
        )
