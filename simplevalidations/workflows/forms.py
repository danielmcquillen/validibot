from __future__ import annotations

import io
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Field
from crispy_forms.layout import Layout
from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from simplevalidations.projects.models import Project
from simplevalidations.submissions.constants import SubmissionFileType
from simplevalidations.validations.constants import JSONSchemaVersion
from simplevalidations.validations.constants import ValidationType
from simplevalidations.validations.constants import XMLSchemaType
from simplevalidations.workflows.models import Workflow
from simplevalidations.workflows.models import WorkflowPublicInfo

if TYPE_CHECKING:
    from simplevalidations.users.models import User
    from simplevalidations.validations.models import Validator


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

MIN_NUMBER_RULE_LINE_PARTS = 2

MAX_SELECTORS = 20

SCHEMA_UPLOAD_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
JSON_SCHEMA_2020_12_URIS = {
    "https://json-schema.org/draft/2020-12/schema",
    "http://json-schema.org/draft/2020-12/schema",
}


def _detect_xml_schema_type(payload: str) -> str | None:
    try:
        from lxml import etree  # noqa: PLC0415
    except Exception:  # pragma: no cover
        return None

    try:
        etree.XMLSchema(etree.XML(payload.encode("utf-8")))
        return XMLSchemaType.XSD.value
    except Exception:
        pass

    try:
        etree.RelaxNG(etree.XML(payload.encode("utf-8")))
        return XMLSchemaType.RELAXNG.value
    except Exception:
        pass

    try:
        etree.DTD(io.StringIO(payload))
        return XMLSchemaType.DTD.value
    except Exception:
        pass

    return None


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
        if len(segments) < MIN_NUMBER_RULE_LINE_PARTS:
            raise RuleParseError(
                _("Rule lines must include at least a path and operator."),
            )
        path = segments[0]
        operator = segments[1].lower()
        value: Any = None
        value_b: Any | None = None

        match operator:
            case "between":
                if len(segments) < 4:  # noqa: PLR2004
                    raise RuleParseError(
                        _("'between' rules require two numeric bounds."),
                    )
                value = segments[2]
                value_b = segments[3]
            case "in" | "not_in":
                if len(segments) < 3:  # noqa: PLR2004
                    raise RuleParseError(
                        _("'%s' rules require a list of options.") % operator,
                    )
                value = _parse_list_literal(" ".join(segments[2:]))
            case "nonempty":
                value = None
            case _:
                if len(segments) < 3:  # noqa: PLR2004
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
    allowed_file_types = forms.MultipleChoiceField(
        label=_("Allowed file types"),
        help_text=_(
            "Choose the submission file types this workflow accepts. "
            "Note that each validator in the workflow will further "
            "constrain the allowed types.",
        ),
        choices=SubmissionFileType.choices,
        widget=forms.CheckboxSelectMultiple,
        required=True,
    )

    class Meta:
        model = Workflow
        fields = [
            "name",
            "slug",
            "project",
            "allowed_file_types",
            "featured_image",
            "version",
            "is_active",
        ]
        help_texts = {
            "version": _("Optional label to help you track iterations."),
            "is_active": _(
                "Disable a workflow to pause new validation runs without removing it.",
            ),
            "allowed_file_types": _(
                "Choose the submission file types this workflow accepts. "
                "Launchers can only upload/run content using these formats."
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
            Field("allowed_file_types"),
            Field("featured_image"),
            Field("version", placeholder="e.g. 1.0"),
            Field("is_active"),
        )
        self._configure_project_field()
        self.fields["is_active"].label = _("Workflow active")
        self.fields["is_active"].help_text = _(
            "When unchecked, teammates can still view the workflow but cannot "
            "launch runs until you reactivate it.",
        )
        self.fields["featured_image"].widget = forms.ClearableFileInput()
        self.fields["featured_image"].widget.attrs.update({"class": "form-control"})
        self.fields["featured_image"].label = _("Featured image")
        self.fields["featured_image"].help_text = _(
            "Optional image shown on the workflow info page.",
        )
        allowed_field = self.fields["allowed_file_types"]
        if self.instance and self.instance.pk:
            allowed_field.initial = list(self.instance.allowed_file_types or [])
        elif not allowed_field.initial:
            allowed_field.initial = [SubmissionFileType.JSON]

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise ValidationError(_("Name is required."))
        return name

    def clean_project(self):
        project = self.cleaned_data.get("project")
        if project is None:
            return None

        expected_org_id = None
        if self.instance and getattr(self.instance, "org_id", None):
            expected_org_id = self.instance.org_id
        elif self.user and getattr(self.user, "is_authenticated", False):
            current_org = self.user.get_current_org()
            expected_org_id = getattr(current_org, "pk", None)

        if expected_org_id and project.org_id != expected_org_id:
            raise ValidationError(
                _("Select a project from your current organization."),
            )
        return project

    def clean_allowed_file_types(self):
        values = self.cleaned_data.get("allowed_file_types") or []
        deduped: list[str] = []
        for value in values:
            if value not in SubmissionFileType.values:
                raise ValidationError(
                    _("'%(value)s' is not a supported file type.") % {"value": value},
                )
            if value not in deduped:
                deduped.append(value)
        if not deduped:
            raise ValidationError(_("Select at least one file type."))
        return deduped

    def _configure_project_field(self):
        project_field = self.fields.get("project")
        if project_field is None:
            return

        project_field.required = False
        project_field.widget = forms.Select(
            attrs={
                "class": "form-select",
            },
        )
        project_field.empty_label = _("Select a project")
        project_field.help_text = _(
            "Workflow runs started from this workflow default to the selected "
            "project. Projects listed belong to your current organization.",
        )
        project_field.queryset = Project.objects.none()

        if not self.user or not getattr(self.user, "is_authenticated", False):
            return

        org = self.user.get_current_org()
        if not org:
            return

        projects = Project.objects.filter(org=org).order_by("name")
        project_field.queryset = projects

        if self.instance and self.instance.pk:
            project_field.initial = self.instance.project_id
            return

        if self.initial.get("project"):
            project_field.initial = self.initial["project"]
            return

        default_project = projects.filter(is_default=True).first() or projects.first()
        if default_project:
            project_field.initial = default_project.pk


class WorkflowLaunchForm(forms.Form):
    filename = forms.CharField(
        label=_("Submission name"),
        required=False,
        help_text=_("Optional name for reporting."),
    )
    file_type = forms.ChoiceField(
        label=_("File type"),
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
        self.single_file_type_label: str | None = None
        self._configure_file_type_field()

    def _configure_file_type_field(self) -> None:
        file_type_field = self.fields["file_type"]
        choices: list[tuple[str, str]] = []
        for value in self.workflow.allowed_file_types or []:
            try:
                label = SubmissionFileType(value).label
            except Exception:
                label = value
            choices.append((value, label))
        file_type_field.choices = choices
        if choices and not file_type_field.initial:
            file_type_field.initial = choices[0][0]
        if len(choices) == 1:
            file_type_field.widget = forms.HiddenInput()
            self.single_file_type_label = choices[0][1]

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
            both_msg = _("Provide inline content or upload a file, not both.")
            self.add_error("payload", both_msg)
            self.add_error("attachment", both_msg)
            raise forms.ValidationError(both_msg)
        if not payload and not attachment:
            missing_msg = _(
                "Add content inline or upload a file before starting the validation.",
            )
            self.add_error("payload", missing_msg)
            self.add_error("attachment", missing_msg)
            raise forms.ValidationError(missing_msg)

        file_type = cleaned.get("file_type")
        allowed_values = set(self.workflow.allowed_file_types or [])
        if file_type not in allowed_values:
            raise forms.ValidationError(
                _("Select a supported file type."),
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
    """Select the kind of workflow step to add (validation or action)."""

    choice = forms.ChoiceField(
        label=_("Step option"),
        widget=forms.RadioSelect,
    )

    def __init__(self, *args, options: list[dict[str, object]], **kwargs):
        super().__init__(*args, **kwargs)
        self.options_by_value = {str(opt["value"]): opt for opt in options}
        self.fields["choice"].choices = [
            (str(opt["value"]), opt["label"]) for opt in options
        ]

    def get_selection(self) -> dict[str, object]:
        value = str(self.cleaned_data.get("choice"))
        return self.options_by_value[value]


class BaseStepConfigForm(forms.Form):
    name = forms.CharField(
        label=_("Step name"),
        max_length=200,
        widget=forms.TextInput(
            attrs={"placeholder": _("Describe what this step checks")},
        ),
    )
    description = forms.CharField(
        label=_("Description"),
        required=False,
        max_length=2000,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": _("In this step, we check that..."),
            },
        ),
        help_text=_("Brief description to help users understand what this step does."),
    )
    display_schema = forms.BooleanField(
        label=_("User can view schema"),
        required=False,
        initial=False,
        help_text=_(
            "When enabled, users can view the schema.",
        ),
    )
    notes = forms.CharField(
        label=_("Author notes"),
        required=False,
        max_length=2000,
        widget=forms.Textarea(
            attrs={
                "rows": 3,
                "placeholder": _("Note to self..."),
            },
        ),
        help_text=_(
            "Author notes about this step (visible only by you and other "
            "users with author permissions for this workflow).",
        ),
    )

    def __init__(self, *args, step=None, **kwargs):
        self.step = step
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.disable_csrf = True
        self.initial_from_step(step)

    def initial_from_step(self, step) -> None:
        if step and step.name:
            self.fields["name"].initial = step.name
        if step and hasattr(step, "description") and step.description:
            self.fields["description"].initial = step.description
        if "display_schema" in self.fields:
            self.fields["display_schema"].initial = bool(
                getattr(step, "display_schema", False),
            )
        if step and hasattr(step, "notes") and step.notes:
            self.fields["notes"].initial = step.notes


class JsonSchemaStepConfigForm(BaseStepConfigForm):
    schema_type = forms.ChoiceField(
        label=_("Schema version"),
        choices=[
            (
                JSONSchemaVersion.DRAFT_2020_12.value,
                JSONSchemaVersion.DRAFT_2020_12.label,
            )
        ],
        initial=JSONSchemaVersion.DRAFT_2020_12.value,
        required=False,
        widget=forms.HiddenInput(),
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
        super().__init__(*args, step=step, **kwargs)
        schema_field = self.fields["schema_type"]
        schema_field.widget = forms.HiddenInput()
        schema_field.required = False
        schema_field.initial = JSONSchemaVersion.DRAFT_2020_12.value
        self.initial["schema_type"] = JSONSchemaVersion.DRAFT_2020_12.value
        if step and step.ruleset_id:
            self.fields["schema_text"].initial = step.config.get(
                "schema_text_preview",
                "",
            )
            self.fields["schema_text"].help_text = _(
                "Leave blank to keep the existing schema. "
                "Paste new JSON to replace it.",
            )
        else:
            self.fields["schema_text"].help_text = _(
                "Paste your JSON schema or upload a file below.",
            )
        self.fields["schema_type"].initial = JSONSchemaVersion.DRAFT_2020_12.value
        self.initial["schema_type"] = JSONSchemaVersion.DRAFT_2020_12.value

    def clean(self):
        cleaned = super().clean()
        text = cleaned.get("schema_text", "").strip()
        file = cleaned.get("schema_file")
        has_text = bool(text)
        has_file = bool(file)

        cleaned["schema_type"] = JSONSchemaVersion.DRAFT_2020_12.value

        if has_text and has_file:
            error = _("Paste the schema or upload a file, not both.")
            self.add_error("schema_text", error)
            self.add_error("schema_file", error)
        if has_file and file.size > SCHEMA_UPLOAD_MAX_BYTES:
            self.add_error(
                "schema_file",
                _("Uploaded schema files must be 2 MB or smaller."),
            )
        if not has_text and not has_file:
            if self.step and self.step.ruleset_id:
                cleaned["schema_source"] = "keep"
            else:
                message = _("Add content directly or upload a file.")
                self.add_error("schema_text", message)
                self.add_error("schema_file", message)
        else:
            cleaned["schema_source"] = "text" if has_text else "upload"
            if has_text:
                cleaned["schema_text"] = text
        source = cleaned.get("schema_source")
        if source in {"text", "upload"}:
            field_name = "schema_text" if source == "text" else "schema_file"
            payload: str | None = None
            if source == "text":
                payload = text
            else:
                upload = cleaned.get("schema_file")
                if upload:
                    upload.seek(0)
                    raw_bytes = upload.read()
                    upload.seek(0)
                    try:
                        payload = raw_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        self.add_error(
                            field_name,
                            _("Uploaded schema must be UTF-8 encoded."),
                        )
                        payload = None
            if payload:
                try:
                    schema_payload = json.loads(payload)
                except json.JSONDecodeError:
                    self.add_error(
                        field_name,
                        _("Schema content must be valid JSON."),
                    )
                else:
                    schema_uri = schema_payload.get("$schema")
                    if schema_uri not in JSON_SCHEMA_2020_12_URIS:
                        self.add_error(
                            field_name,
                            _("JSON schemas must declare $schema as Draft 2020-12."),
                        )
        return cleaned
        return cleaned


class XmlSchemaStepConfigForm(BaseStepConfigForm):
    schema_type = forms.ChoiceField(
        label=_("Schema type"),
        choices=XMLSchemaType.choices,
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
        super().__init__(*args, step=step, **kwargs)
        if step and step.ruleset_id:
            current_schema_type = None
            if step.ruleset:
                current_schema_type = (step.ruleset.metadata or {}).get("schema_type")
            if current_schema_type in XMLSchemaType.values:
                self.fields["schema_type"].initial = current_schema_type
            elif (
                step
                and step.config
                and step.config.get("schema_type") in XMLSchemaType.values
            ):
                self.fields["schema_type"].initial = step.config.get("schema_type")
            self.fields["schema_text"].initial = step.config.get(
                "schema_text_preview",
                "",
            )
            self.fields["schema_text"].help_text = _(
                "Leave blank to keep the existing schema. Paste new XML to replace it.",
            )
        else:
            self.fields["schema_text"].help_text = _(
                "Paste your XML schema or upload a file below.",
            )

    def clean(self):
        cleaned = super().clean()
        text = cleaned.get("schema_text", "").strip()
        file = cleaned.get("schema_file")
        has_text = bool(text)
        has_file = bool(file)

        if has_text and has_file:
            error = _("Paste the schema or upload a file, not both.")
            self.add_error("schema_text", error)
            self.add_error("schema_file", error)
        if has_file and file.size > SCHEMA_UPLOAD_MAX_BYTES:
            self.add_error(
                "schema_file",
                _("Uploaded schema files must be 2 MB or smaller."),
            )
        if not has_text and not has_file:
            if self.step and self.step.ruleset_id:
                cleaned["schema_source"] = "keep"
            else:
                message = _("Add content directly or upload a file.")
                self.add_error("schema_text", message)
                self.add_error("schema_file", message)
        else:
            cleaned["schema_source"] = "text" if has_text else "upload"
            if has_text:
                cleaned["schema_text"] = text
        selected_type = (cleaned.get("schema_type") or "").upper()
        source = cleaned.get("schema_source")
        if source in {"text", "upload"}:
            field_name = "schema_text" if source == "text" else "schema_file"
            payload: str | None = None
            if source == "text":
                payload = text
            else:
                upload = cleaned.get("schema_file")
                if upload:
                    upload.seek(0)
                    raw_bytes = upload.read()
                    upload.seek(0)
                    try:
                        payload = raw_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        self.add_error(
                            field_name,
                            _("Uploaded schema must be UTF-8 encoded."),
                        )
                        payload = None
            if payload:
                detected_type = _detect_xml_schema_type(payload)
                if not detected_type:
                    expected_label = (
                        XMLSchemaType(selected_type).label
                        if selected_type in XMLSchemaType.values
                        else _("XML schema")
                    )
                    self.add_error(
                        field_name,
                        _(
                            "Unable to parse the XML schema. Ensure it matches the %(expected)s format."
                        )
                        % {"expected": expected_label},
                    )
                elif selected_type and detected_type != selected_type:
                    detected_label = XMLSchemaType(detected_type).label
                    selected_label = XMLSchemaType(selected_type).label
                    message = _(
                        "Uploaded schema appears to be %(detected)s but you selected %(selected)s."
                    ) % {"detected": detected_label, "selected": selected_label}
                    self.add_error(field_name, message)
                    self.add_error("schema_type", message)
        return cleaned


class EnergyPlusStepConfigForm(BaseStepConfigForm):
    """Collects EnergyPlus step options including optional simulation checks.

    Example:
        form = EnergyPlusStepConfigForm(data={"run_simulation": True})
    """

    idf_checks = forms.MultipleChoiceField(
        label=_("Initial IDF checks"),
        required=False,
        choices=ENERGYPLUS_IDF_CHECK_CHOICES,
        widget=forms.CheckboxSelectMultiple,
    )
    run_simulation = forms.BooleanField(
        label=_("Run EnergyPlus simulation"),
        help_text=_(
            "If this option is unchecked, only IDF syntax checks will be performed.",
        ),
        required=False,
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
    energyplus_notes = forms.CharField(
        label=_("EnergyPlus notes"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, step=None, **kwargs):
        super().__init__(*args, step=step, **kwargs)
        self.fields.pop("display_schema", None)
        if step:
            config = step.config or {}
            self.initial.update(
                {
                    "run_simulation": config.get("run_simulation", False),
                    "idf_checks": config.get("idf_checks", []),
                    "simulation_checks": config.get("simulation_checks", []),
                    "eui_min": config.get("eui_band", {}).get("min"),
                    "eui_max": config.get("eui_band", {}).get("max"),
                    "energyplus_notes": config.get("notes", ""),
                }
            )
            for key, value in self.initial.items():
                if key in self.fields and value not in (None, ""):
                    self.fields[key].initial = value

    def clean(self):
        cleaned = super().clean()
        eui_min = cleaned.get("eui_min")
        eui_max = cleaned.get("eui_max")
        run_simulation = cleaned.get("run_simulation")
        simulation_checks = cleaned.get("simulation_checks") or []
        if not run_simulation and simulation_checks:
            self.add_error(
                "simulation_checks",
                _("Enable 'Run EnergyPlus simulation' to use post-simulation checks."),
            )
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
                    "$.zones[*].cooling_setpoint >= 18 | "
                    "Cooling setpoint must be ≥18°C",
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
        super().__init__(*args, step=step, **kwargs)
        self.fields.pop("display_schema", None)
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
        if len(selectors) > MAX_SELECTORS:
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
        ValidationType.BASIC: BasicStepConfigForm,
        ValidationType.JSON_SCHEMA: JsonSchemaStepConfigForm,
        ValidationType.XML_SCHEMA: XmlSchemaStepConfigForm,
        ValidationType.ENERGYPLUS: EnergyPlusStepConfigForm,
        ValidationType.AI_ASSIST: AiAssistStepConfigForm,
    }
    return mapping.get(validation_type, BaseStepConfigForm)


class WorkflowPublicInfoForm(forms.ModelForm):
    make_info_public = forms.BooleanField(
        label=_("Make info public"),
        required=False,
        help_text=_(
            "When enabled, anyone with the link can view the workflow’s info page.",
        ),
    )

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
        self.fields["make_info_public"].initial = bool(workflow.make_info_public)
        self.fields["make_info_public"].widget.attrs.setdefault(
            "class",
            "form-check-input",
        )
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Field("title"),
            Field("content_md"),
            Field("make_info_public"),
        )


class BasicStepConfigForm(BaseStepConfigForm):
    """Minimal form for manual assertion steps (name/description/notes only)."""

    def __init__(self, *args, step=None, **kwargs):
        super().__init__(*args, step=step, **kwargs)
        self.fields.pop("display_schema", None)
