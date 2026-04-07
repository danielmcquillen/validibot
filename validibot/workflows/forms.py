from __future__ import annotations

import io
import json
import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import HTML
from crispy_forms.layout import Column
from crispy_forms.layout import Div
from crispy_forms.layout import Field
from crispy_forms.layout import Layout
from crispy_forms.layout import Row
from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from validibot.projects.models import Project
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import ValidationType
from validibot.validations.constants import XMLSchemaType
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowPublicInfo
from validibot.workflows.models import WorkflowSignalMapping

if TYPE_CHECKING:
    from validibot.users.models import User

logger = logging.getLogger(__name__)

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

TEMPLATE_VARIABLE_TYPE_CHOICES = (
    ("number", _("Number")),
    ("text", _("Text")),
    ("choice", _("Choice")),
)

MIN_NUMBER_RULE_LINE_PARTS = 2

MAX_SELECTORS = 20

SCHEMA_UPLOAD_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
JSON_SCHEMA_2020_12_URIS = {
    "https://json-schema.org/draft/2020-12/schema",
    "http://json-schema.org/draft/2020-12/schema",
}


def _detect_xml_schema_type(payload: str) -> str | None:
    """
    Best-effort detection of an XML schema type for uploaded content.

    The function tries to parse the payload once and then construct each schema
    validator in turn. Expected/benign exceptions:
    - ImportError (or similar) if ``lxml`` is unavailable: we return ``None``.
    - ``etree.XML`` parse errors: malformed XML, so we bail out and return ``None``.
    - Validator construction errors (XSD/RELAXNG/DTD): treated as “not that type”
      and logged at info level, continuing to the next detector.

    We only surface a value when a validator successfully instantiates; otherwise
    callers receive ``None`` and should handle the absence of a detected schema.
    """
    payload_bytes = payload.encode("utf-8")
    if len(payload_bytes) > SCHEMA_UPLOAD_MAX_BYTES:
        logger.info("XML schema detection skipped: payload exceeds size limit.")
        return None

    try:
        from lxml import etree
    except Exception:  # pragma: no cover
        return None

    xml_doc = None
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=False)
        xml_doc = etree.XML(payload_bytes, parser=parser)
    except Exception:
        logger.info("Could not detect schema type via XML parsing.")

    if xml_doc is not None:
        try:
            etree.XMLSchema(xml_doc)
        except Exception:
            logger.info("XML Schema detection failed for XSD.")
        else:
            return XMLSchemaType.XSD.value

        try:
            etree.RelaxNG(xml_doc)
        except Exception:
            logger.info("XML Schema detection failed for RELAXNG.")
        else:
            return XMLSchemaType.RELAXNG.value

    try:
        etree.DTD(io.StringIO(payload))
    except Exception:
        logger.info("XML Schema detection failed for DTD.")
        return None
    return XMLSchemaType.DTD.value


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
    """Author workflows and their optional structured JSON input contract.

    The form keeps the canonical runtime contract on ``Workflow.input_schema``
    while preserving the author's preferred editing representation
    (JSON Schema or restricted Pydantic text) for round-trip editing.
    """

    description_md = forms.CharField(
        label=_("Public info page description (Markdown)"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 6,
                "placeholder": _(
                    "Optional: a more detailed description for the public info page...",
                ),
            },
        ),
        help_text=_(
            "Use this to provide a separate, more involved description for the "
            "workflow's public info page. Supports Markdown formatting. "
            "Leave blank to use the standard description above."
        ),
    )
    allowed_file_types = forms.MultipleChoiceField(
        label=_("Allowed file types"),
        help_text=_(
            "Choose the submission file types this workflow accepts. "
            "Note that each validator in the workflow may further "
            "constrain the allowed types.",
        ),
        choices=SubmissionFileType.choices,
        widget=forms.CheckboxSelectMultiple,
        required=True,
    )

    # ── Input contract authoring fields ──────────────────────────────────
    # These are non-model fields that drive the authoring UI.  The clean()
    # method converts the author's input into the canonical JSON Schema
    # stored on Workflow.input_schema.

    input_schema_mode = forms.ChoiceField(
        label=_("Input contract mode"),
        choices=[
            ("", _("None")),
            ("json_schema", _("JSON Schema")),
            ("pydantic", _("Pydantic")),
        ],
        widget=forms.RadioSelect(
            attrs={"class": "form-check-input"},
        ),
        required=False,
        help_text=_(
            "Choose how to define the input contract.  Both modes produce the "
            "same canonical JSON Schema stored on the workflow.  "
            "Select 'None' to remove the input contract."
        ),
    )

    input_schema_json = forms.CharField(
        label=_("JSON Schema"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 12,
                "class": "form-control font-monospace",
                "placeholder": _(
                    "{\n"
                    '  "type": "object",\n'
                    '  "properties": {\n'
                    '    "wall_r_value": {\n'
                    '      "type": "number",\n'
                    '      "description": "Total wall R-value",\n'
                    '      "minimum": 0\n'
                    "    }\n"
                    "  },\n"
                    '  "required": ["wall_r_value"]\n'
                    "}"
                ),
            },
        ),
        help_text=_(
            "Paste a JSON Schema document with a flat 'properties' object.  "
            "Supported types: string, integer, number, boolean.  "
            "The stored contract is always canonical JSON Schema."
        ),
    )

    input_schema_pydantic = forms.CharField(
        label=_("Pydantic model"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 12,
                "class": "form-control font-monospace",
                "placeholder": _(
                    "class SectionJInput(BaseModel):\n"
                    "    climate_zone: int = Field("
                    'description="NCC Climate Zone", ge=1, le=8)\n'
                    "    wall_r_value: float = Field(\n"
                    '        description="Total wall R-value",\n'
                    "        gt=0,\n"
                    '        json_schema_extra={"units": "m²K/W"},\n'
                    "    )"
                ),
            },
        ),
        help_text=_(
            "Paste a single BaseModel class using a restricted Pydantic 2 subset.  "
            "Supported types: str, int, float, bool, Optional[...], Literal[...].  "
            "Supported Field() kwargs: description, default, ge, gt, le, lt, "
            "json_schema_extra.  Methods, validators, and nested models are rejected."
        ),
    )

    class Meta:
        model = Workflow
        fields = [
            "name",
            "description",
            "slug",
            "project",
            "allowed_file_types",
            "input_schema",
            "input_schema_source_mode",
            "input_schema_source_text",
            "data_retention",
            "success_message",
            "allow_submission_name",
            "allow_submission_meta_data",
            "allow_submission_short_description",
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
            "data_retention": _(
                "Controls how long the user's submission data is kept after "
                "validation. The submission record is always preserved for "
                "audit purposes."
            ),
        }

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
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
        # Configure data retention field
        self.fields["data_retention"].label = _("Data retention")
        self.fields["data_retention"].widget.attrs.update({"class": "form-select"})
        # Configure success message field
        self.fields["success_message"].label = _("Success message")
        self.fields["success_message"].help_text = _(
            "Custom message shown when validation succeeds. "
            "Leave blank to use the default message."
        )
        self.fields["success_message"].widget = forms.Textarea(
            attrs={
                "rows": 2,
                "class": "form-control",
                "placeholder": _("e.g. Your model passed all validation checks!"),
            },
        )
        self.fields["description_md"].widget.attrs.setdefault("class", "form-control")
        if self.instance and self.instance.pk:
            self.fields["description_md"].initial = (
                self.instance.get_public_info.content_md or ""
            )

        # ── Input contract: populate authoring fields from stored data ───
        # The model fields (input_schema, input_schema_source_mode,
        # input_schema_source_text) are hidden; the non-model authoring
        # fields drive the UI.
        self.fields["input_schema"].widget = forms.HiddenInput()
        self.fields["input_schema_source_mode"].widget = forms.HiddenInput()
        self.fields["input_schema_source_text"].widget = forms.HiddenInput()
        self.fields["input_schema"].required = False
        self.fields["input_schema_source_mode"].required = False
        self.fields["input_schema_source_text"].required = False
        self.fields["input_schema_mode"].widget.attrs[
            "data-input-schema-mode-field"
        ] = "true"
        self.fields["input_schema_json"].widget.attrs["data-input-schema-editor"] = (
            "json_schema"
        )
        self.fields["input_schema_pydantic"].widget.attrs[
            "data-input-schema-editor"
        ] = "pydantic"

        if self.instance and self.instance.pk and self.instance.input_schema:
            mode = self.instance.input_schema_source_mode or "json_schema"
            source_text = self.instance.input_schema_source_text
            self.fields["input_schema_mode"].initial = mode
            if mode == "pydantic" and source_text:
                self.fields["input_schema_pydantic"].initial = source_text
            else:
                # Default to showing the canonical JSON Schema
                import json as _json

                self.fields["input_schema_json"].initial = _json.dumps(
                    self.instance.input_schema,
                    indent=2,
                )

        # ── Superuser-only agent access fields ──────────────────────────
        # These fields are not in Meta.fields — they are added dynamically
        # so that only superusers can see and edit them.
        if self.user and getattr(self.user, "is_superuser", False):
            self._add_agent_fields()

        self.helper.layout = self._build_layout()

    def _add_agent_fields(self) -> None:
        """Add agent access fields to the form for superusers."""
        from validibot.workflows.constants import AgentBillingMode

        self.fields["agent_access_enabled"] = forms.BooleanField(
            required=False,
            label=_("Agent access enabled"),
            help_text=_(
                "Expose this workflow for agent access via MCP.",
            ),
        )
        if self.instance and self.instance.pk:
            self.fields[
                "agent_access_enabled"
            ].initial = self.instance.agent_access_enabled

        self.fields["agent_billing_mode"] = forms.ChoiceField(
            choices=AgentBillingMode.choices,
            initial=AgentBillingMode.AUTHOR_PAYS,
            label=_("Agent billing mode"),
            help_text=_(
                "Who pays when an agent invokes this workflow.",
            ),
            widget=forms.Select(attrs={"class": "form-select"}),
        )
        if self.instance and self.instance.pk:
            self.fields["agent_billing_mode"].initial = self.instance.agent_billing_mode

        self.fields["agent_price_cents"] = forms.IntegerField(
            required=False,
            min_value=1,
            label=_("Price per invocation (US cents)"),
            help_text=_(
                "USDC equivalent. Required when billing mode is x402.",
            ),
            widget=forms.NumberInput(attrs={"class": "form-control"}),
        )
        if self.instance and self.instance.pk:
            self.fields["agent_price_cents"].initial = self.instance.agent_price_cents

        self.fields["agent_max_launches_per_hour"] = forms.IntegerField(
            required=False,
            min_value=1,
            label=_("Max launches per hour"),
            help_text=_(
                "Per-wallet rate limit. Leave blank for platform default.",
            ),
            widget=forms.NumberInput(attrs={"class": "form-control"}),
        )
        if self.instance and self.instance.pk:
            self.fields[
                "agent_max_launches_per_hour"
            ].initial = self.instance.agent_max_launches_per_hour

    def _build_layout(self) -> Layout:
        """Build the crispy layout used by the workflow create/edit page."""
        sections = [
            Div(
                self._section_intro(
                    _("Workflow basics"),
                    _(
                        "Name the workflow, choose its project, and provide the "
                        "descriptions shown in internal and public UI."
                    ),
                ),
                Field("name", placeholder=_("Name your workflow"), autofocus=True),
                Field(
                    "description",
                    placeholder=_("Brief description of what this workflow validates"),
                    rows=3,
                ),
                Field("description_md"),
                Field("slug", placeholder=""),
                Field("project"),
                css_class="border rounded-3 p-3 mb-4",
            ),
            Div(
                self._section_intro(
                    _("Submission settings"),
                    _(
                        "Define which file types the workflow accepts and how "
                        "submission metadata should behave."
                    ),
                ),
                Field("allowed_file_types"),
                Field("data_retention"),
                Field("success_message"),
                Field("allow_submission_name"),
                Field("allow_submission_meta_data"),
                Field("allow_submission_short_description"),
                css_class="border rounded-3 p-3 mb-4",
            ),
            Div(
                self._section_intro(
                    _("Input contract"),
                    _(
                        "Define a structured input schema for JSON-only workflows. "
                        "Choose an authoring mode first, then edit only that "
                        "representation."
                    ),
                ),
                Field("input_schema_mode"),
                Div(
                    HTML(
                        (
                            '<div class="alert alert-light small mb-0">'
                            f"{
                                _(
                                    'Choose JSON Schema or Pydantic to start authoring '
                                    'the input contract.'
                                )
                            }"
                            "</div>"
                        ),
                    ),
                    css_class="mb-3",
                    data_input_schema_mode_hint="true",
                ),
                Div(
                    HTML(
                        (
                            '<div class="mb-3">'
                            f'<h6 class="mb-1">{_("JSON Schema editor")}</h6>'
                            f'<p class="text-muted small mb-0">'
                            f"{
                                _(
                                    'Use this when you want to paste or edit the '
                                    'canonical schema directly.'
                                )
                            }"
                            "</p>"
                            "</div>"
                        ),
                    ),
                    Field("input_schema_json"),
                    css_id="input-schema-json-wrapper",
                    css_class="border rounded-3 p-3 mb-3",
                    data_input_schema_mode_value="json_schema",
                ),
                Div(
                    HTML(
                        (
                            '<div class="mb-3">'
                            f'<h6 class="mb-1">{_("Pydantic editor")}</h6>'
                            f'<p class="text-muted small mb-0">'
                            f"{
                                _(
                                    'Use this when you want to author the contract '
                                    'as a restricted BaseModel class and let '
                                    'Validibot convert it to canonical JSON Schema.'
                                )
                            }"
                            "</p>"
                            "</div>"
                        ),
                    ),
                    Field("input_schema_pydantic"),
                    css_id="input-schema-pydantic-wrapper",
                    css_class="border rounded-3 p-3 mb-3",
                    data_input_schema_mode_value="pydantic",
                ),
                # Hidden model fields — populated by clean()
                Field("input_schema", type="hidden"),
                Field("input_schema_source_mode", type="hidden"),
                Field("input_schema_source_text", type="hidden"),
                css_class="border rounded-3 p-3 mb-4",
                data_input_schema_section="true",
            ),
            Div(
                self._section_intro(
                    _("Publishing"),
                    _(
                        "Control visibility, featured artwork, and the version label "
                        "shown to your team."
                    ),
                ),
                Field("featured_image"),
                Field("version", placeholder="e.g. 1.0"),
                Field("is_active"),
                css_class="border rounded-3 p-3 mb-4",
            ),
        ]

        if self.user and getattr(self.user, "is_superuser", False):
            sections.append(
                Div(
                    self._section_intro(
                        _("Agent access"),
                        _(
                            "Control how AI agents discover and invoke this "
                            "workflow via MCP. Only visible to superusers."
                        ),
                    ),
                    Field("agent_access_enabled"),
                    Field("agent_billing_mode"),
                    Field("agent_price_cents"),
                    Field("agent_max_launches_per_hour"),
                    css_class="border border-warning rounded-3 p-3",
                ),
            )

        return Layout(*sections)

    def _section_intro(self, title: str, body: str) -> HTML:
        """Render a compact section heading for the crispy form layout."""
        return HTML(
            '<div class="mb-3">'
            f'<h6 class="mb-1">{title}</h6>'
            f'<p class="text-muted small mb-0">{body}</p>'
            "</div>",
        )

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

    def clean(self):
        """Run the input-contract authoring pipeline.

        If the author provided input contract text in either mode, this
        method converts it to canonical JSON Schema, validates the supported
        v1 subset, and writes the result into the hidden model fields.
        """
        cleaned = super().clean()
        mode = cleaned.get("input_schema_mode", "")
        json_text = (cleaned.get("input_schema_json") or "").strip()
        pydantic_text = (cleaned.get("input_schema_pydantic") or "").strip()
        allowed = cleaned.get("allowed_file_types") or []

        if not mode:
            if json_text or pydantic_text:
                self.add_error(
                    "input_schema_mode",
                    ValidationError(
                        _(
                            "Choose JSON Schema or Pydantic before saving an "
                            "input contract."
                        ),
                        code="missing_input_schema_mode",
                    ),
                )
                return cleaned

            # No input contract requested — clear the model fields
            cleaned["input_schema"] = None
            cleaned["input_schema_source_mode"] = ""
            cleaned["input_schema_source_text"] = ""
            return cleaned

        # Input contract only valid for JSON-only workflows
        if set(allowed) != {SubmissionFileType.JSON}:
            self.add_error(
                "input_schema_mode",
                ValidationError(
                    _(
                        "Input contracts are only supported when the sole "
                        "allowed file type is JSON."
                    ),
                    code="not_json_only",
                ),
            )
            return cleaned

        from validibot.workflows.schema_authoring import parse_json_schema_input
        from validibot.workflows.schema_authoring import parse_pydantic_input

        schema = None
        source_text = ""

        if mode == "json_schema":
            if not json_text:
                self.add_error(
                    "input_schema_json",
                    ValidationError(
                        _(
                            "Paste a JSON Schema document or select 'None' "
                            "to remove the input contract."
                        ),
                        code="empty_json_schema",
                    ),
                )
                return cleaned
            try:
                schema = parse_json_schema_input(json_text)
            except ValidationError as exc:
                self.add_error("input_schema_json", exc)
                return cleaned
            source_text = json_text

        elif mode == "pydantic":
            if not pydantic_text:
                self.add_error(
                    "input_schema_pydantic",
                    ValidationError(
                        _(
                            "Paste a Pydantic BaseModel class or select 'None' "
                            "to remove the input contract."
                        ),
                        code="empty_pydantic",
                    ),
                )
                return cleaned
            try:
                schema = parse_pydantic_input(pydantic_text)
            except ValidationError as exc:
                self.add_error("input_schema_pydantic", exc)
                return cleaned
            source_text = pydantic_text

        cleaned["input_schema"] = schema
        cleaned["input_schema_source_mode"] = mode
        cleaned["input_schema_source_text"] = source_text
        return cleaned

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

    def save(self, *, commit: bool = True):
        workflow = super().save(commit=commit)

        # Write superuser-only agent fields that are not in Meta.fields.
        if self.user and getattr(self.user, "is_superuser", False):
            agent_fields = [
                "agent_access_enabled",
                "agent_billing_mode",
                "agent_price_cents",
                "agent_max_launches_per_hour",
            ]
            for field_name in agent_fields:
                if field_name in self.cleaned_data:
                    setattr(workflow, field_name, self.cleaned_data[field_name])
            if commit and workflow.pk:
                workflow.save(update_fields=agent_fields)

        if commit and workflow.pk:
            description_md = (self.cleaned_data.get("description_md") or "").strip()
            public_info = workflow.get_public_info
            if public_info.content_md != description_md:
                public_info.content_md = description_md
                public_info.save()
        return workflow


class WorkflowLaunchForm(forms.Form):
    filename = forms.CharField(
        label=_("Submission name"),
        required=False,
        help_text=_("Optional name for reporting and/or verifiable credentials."),
    )
    file_type = forms.ChoiceField(
        label=_("File type"),
        choices=[],
    )
    payload = forms.CharField(
        label=_("Submission data"),
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
        help_text=_("Upload a file instead of pasting submission data."),
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
    short_description = forms.CharField(
        label=_("Short description"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _("Brief context for this submission"),
            },
        ),
        help_text=_("Optional short description stored with the submission."),
    )

    def __init__(self, *args, workflow: Workflow, user: User | None = None, **kwargs):
        self.workflow = workflow
        self.user = user
        super().__init__(*args, **kwargs)
        self._apply_bootstrap_styles()
        self.single_file_type_label: str | None = None
        self._configure_file_type_field()
        self._configure_optional_fields()

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
            file_type_field.label = _("Required file type")
            self.single_file_type_label = choices[0][1]

    def _apply_bootstrap_styles(self) -> None:
        for name, field in self.fields.items():
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
            if name == "attachment":
                current_class = widget.attrs.get("class", "")
                widget.attrs.update(
                    {
                        "data-dropzone-input": "true",
                        "class": f"{current_class} visually-hidden".strip(),
                    },
                )
            if name in {"filename", "metadata", "short_description"}:
                widget.attrs["data-launch-extra-field"] = name

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
                "Paste in content or upload a file before starting the validation.",
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

        # Validate file extension for uploads
        if attachment:
            from validibot.validations.models import get_allowed_extensions_for_workflow

            filename = getattr(attachment, "name", "") or ""
            ext = ""
            if "." in filename:
                ext = filename.rsplit(".", 1)[-1].lower()
            allowed_extensions = get_allowed_extensions_for_workflow(self.workflow)
            if allowed_extensions and ext not in allowed_extensions:
                ext_list = ", ".join(sorted(f".{e}" for e in allowed_extensions))
                self.add_error(
                    "attachment",
                    _(
                        "File extension '.%(ext)s' is not allowed. "
                        "Accepted extensions: %(allowed)s"
                    )
                    % {"ext": ext, "allowed": ext_list},
                )

        cleaned["payload"] = payload

        metadata = cleaned.get("metadata")
        if self.workflow.allow_submission_meta_data:
            if metadata:
                try:
                    cleaned["metadata"] = json.loads(metadata)
                except json.JSONDecodeError as exc:
                    raise forms.ValidationError(
                        _("Metadata must be valid JSON."),
                    ) from exc
            else:
                cleaned["metadata"] = {}
        else:
            cleaned["metadata"] = {}

        short_description = (cleaned.get("short_description") or "").strip()
        cleaned["short_description"] = (
            short_description
            if self.workflow.allow_submission_short_description
            else ""
        )

        if not self.workflow.allow_submission_name:
            cleaned["filename"] = ""

        return cleaned

    def _configure_optional_fields(self) -> None:
        """Hide optional fields based on workflow configuration."""

        if not self.workflow.allow_submission_name:
            self.fields["filename"].widget = forms.HiddenInput()

        if not self.workflow.allow_submission_meta_data:
            self.fields["metadata"].widget = forms.HiddenInput()

        if not self.workflow.allow_submission_short_description:
            self.fields["short_description"].widget = forms.HiddenInput()


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
    show_display_schema = False
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
    show_success_messages = forms.BooleanField(
        label=_("Show success messages for passed assertions"),
        required=False,
        initial=False,
        help_text=_(
            "When enabled, all assertions in this step will return a success message "
            "when they pass. If an assertion has no custom success message, a default "
            "message will be shown."
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

    def __init__(self, *args, step=None, org=None, validator=None, **kwargs):
        self.step = step
        self.org = org
        self.validator = validator
        super().__init__(*args, **kwargs)
        if not self.show_display_schema:
            self.fields.pop("display_schema", None)
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
        if "show_success_messages" in self.fields:
            self.fields["show_success_messages"].initial = bool(
                getattr(step, "show_success_messages", False),
            )
        if step and hasattr(step, "notes") and step.notes:
            self.fields["notes"].initial = step.notes


class FMUValidatorStepConfigForm(BaseStepConfigForm):
    """FMU step configuration form.

    Supports two modes, selected automatically based on the validator:

    - **Library validator**: The FMU is already attached to the validator
      via ``validator.fmu_model``.  No upload fields are shown — signals
      come from the validator's SignalDefinition rows.

    - **System FMU validator (step-level upload)**: The author uploads
      an FMU directly in the step form.  The system introspects the FMU
      and stores discovered variables as ``SignalDefinition`` rows
      and simulation defaults in ``step.config["fmu_simulation"]``.

    See ADR-2026-03-12: Step-Level FMU Upload for Workflow Authors.
    """

    # ── FMU upload ────────────────────────────────────────────────
    fmu_file = forms.FileField(
        label=_("FMU file"),
        required=False,
        help_text=_(
            "Upload an FMU file (.fmu). Input and output variables will "
            "be auto-detected from modelDescription.xml."
        ),
    )
    remove_fmu = forms.BooleanField(
        required=False,
        widget=forms.HiddenInput,
    )

    # ── Simulation settings ──────────────────────────────────────
    # Pre-populated from the FMU's DefaultExperiment when available.
    sim_start_time = forms.FloatField(
        label=_("Start time (s)"),
        required=False,
        help_text=_(
            "When the simulation begins (in seconds). Usually 0. "
            "Auto-detected from the FMU if available."
        ),
    )
    sim_stop_time = forms.FloatField(
        label=_("Stop time (s)"),
        required=False,
        help_text=_(
            "When the simulation ends (in seconds). For example, 3600 = one hour. "
            "Auto-detected from the FMU if available."
        ),
    )
    sim_step_size = forms.FloatField(
        label=_("Step size (s)"),
        required=False,
        min_value=0.0001,
        help_text=_(
            "How often results are exchanged during the simulation (in seconds). "
            "Smaller values give more detail but take longer. "
            "Auto-detected from the FMU if available."
        ),
    )
    sim_tolerance = forms.FloatField(
        label=_("Tolerance"),
        required=False,
        min_value=0,
        help_text=_(
            "Solver accuracy. Smaller values (e.g. 1e-6) are more "
            "precise but slower. Auto-detected from the FMU if "
            "available. Leave blank for the solver default."
        ),
    )

    def __init__(self, *args, step=None, org=None, validator=None, **kwargs):
        super().__init__(*args, step=step, org=org, validator=validator, **kwargs)
        self.fields.pop("display_schema", None)

        # Determine whether this is a system FMU validator (step-level
        # upload path) or a library validator (catalog path).
        self.is_system_validator = getattr(validator, "is_system", False)

        # Template state for display in the form
        self.has_fmu = False
        self.fmu_filename = ""

        # Hide upload fields for library validators — the FMU is
        # already attached to the validator.
        if not self.is_system_validator:
            self.fields.pop("fmu_file", None)
            self.fields.pop("remove_fmu", None)
            self.fields.pop("sim_start_time", None)
            self.fields.pop("sim_stop_time", None)
            self.fields.pop("sim_step_size", None)
            self.fields.pop("sim_tolerance", None)
            self.helper.layout = Layout(
                "name",
                "description",
                "show_success_messages",
                "notes",
            )
            return

        # Pre-populate simulation fields from step config
        if step:
            from validibot.workflows.models import WorkflowStepResource

            config = step.config or {}
            sim = config.get("fmu_simulation") or {}
            if sim.get("start_time") is not None:
                self.fields["sim_start_time"].initial = sim["start_time"]
            if sim.get("stop_time") is not None:
                self.fields["sim_stop_time"].initial = sim["stop_time"]
            if sim.get("step_size") is not None:
                self.fields["sim_step_size"].initial = sim["step_size"]
            if sim.get("tolerance") is not None:
                self.fields["sim_tolerance"].initial = sim["tolerance"]

            # Check for existing FMU resource
            fmu_resource = step.step_resources.filter(
                role=WorkflowStepResource.FMU_MODEL,
            ).first()
            if fmu_resource:
                self.has_fmu = True
                self.fmu_filename = fmu_resource.filename or ""

        # ── Crispy Layout ─────────────────────────────────────────
        self.helper.layout = Layout(
            "name",
            "description",
            "show_success_messages",
            "fmu_file",
            "remove_fmu",
            Div(
                HTML(
                    '<h3 class="h6 text-muted mt-3 mb-2">'
                    "Simulation Settings"
                    "</h3>"
                    '<p class="text-muted small mb-3">'
                    "These control how long and how precisely the FMU runs. "
                    "Values are auto-detected from the FMU when you upload it. "
                    "Override them here if needed."
                    "</p>"
                ),
                "sim_start_time",
                "sim_stop_time",
                "sim_step_size",
                "sim_tolerance",
                css_class="fmu-simulation-settings",
            ),
            "notes",
        )


class JsonSchemaStepConfigForm(BaseStepConfigForm):
    show_display_schema = True
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
    show_display_schema = True
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
                            "Unable to parse the XML schema. Ensure it "
                            "matches the %(expected)s format."
                        )
                        % {"expected": expected_label},
                    )
                elif selected_type and detected_type != selected_type:
                    detected_label = XMLSchemaType(detected_type).label
                    selected_label = XMLSchemaType(selected_type).label
                    message = _(
                        "Uploaded schema appears to be %(detected)s "
                        "but you selected %(selected)s."
                    ) % {"detected": detected_label, "selected": selected_label}
                    self.add_error(field_name, message)
                    self.add_error("schema_type", message)
        return cleaned


class EnergyPlusStepConfigForm(BaseStepConfigForm):
    """Collects EnergyPlus step configuration options.

    The form presents two validation modes via the ``validation_mode`` field:

    - **direct**: Users submit a complete IDF file.  The form shows
      IDF-check and simulation options.
    - **template**: Users submit JSON parameter values.  The form shows
      template upload, case-sensitivity, and signal-selection options.

    Client-side JavaScript toggles the visibility of mode-specific field
    groups.  On the server side, ``build_energyplus_config()`` reads the
    selected mode and only processes the relevant cleaned data.

    The template *file* is stored on ``WorkflowStepResource``
    (role=MODEL_TEMPLATE); the template *configuration* (variables, case
    sensitivity) is stored in step config and built by
    ``build_energyplus_config()`` in ``views_helpers.py``.

    Example:
        form = EnergyPlusStepConfigForm(
            data={"validation_mode": "direct", "run_simulation": True},
            files=request.FILES,
            org=my_org,
            validator=energyplus_validator,
        )
    """

    # ── Mode selector ─────────────────────────────────────────────
    VALIDATION_MODE_DIRECT = "direct"
    VALIDATION_MODE_TEMPLATE = "template"
    VALIDATION_MODE_CHOICES = (
        (
            VALIDATION_MODE_DIRECT,
            _("Validate submitted EnergyPlus IDF"),
        ),
        (
            VALIDATION_MODE_TEMPLATE,
            _("Validate values using EnergyPlus template"),
        ),
    )

    validation_mode = forms.ChoiceField(
        label=_("What does this step validate?"),
        choices=VALIDATION_MODE_CHOICES,
        widget=forms.RadioSelect,
        initial=VALIDATION_MODE_DIRECT,
        help_text=_(
            "Choose 'Validate submitted EnergyPlus IDF' if submitters will "
            "upload a complete IDF file for validation. Choose 'Validate values "
            "using EnergyPlus template' if you want to provide a pre-built IDF "
            "with $VARIABLE placeholders and have submitters supply only the "
            "parameter values as JSON."
        ),
    )

    # ── Shared fields ─────────────────────────────────────────────
    weather_file = forms.ChoiceField(
        label=_("Weather file"),
        choices=[],
        help_text=_(
            "Weather file (EPW) used for EnergyPlus simulations. "
            "This determines the climate data for the simulation."
        ),
    )

    show_energyplus_warnings = forms.BooleanField(
        label=_("Show EnergyPlus warnings"),
        required=False,
        initial=True,
        help_text=_(
            "Include EnergyPlus simulation warnings in the results shown to "
            "submitters. Uncheck to show only errors. Warnings can be noisy "
            "for submitters who don't need to debug the model."
        ),
    )

    # ── Direct-mode fields ────────────────────────────────────────
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

    # ── Template-mode fields ──────────────────────────────────────
    template_file = forms.FileField(
        label=_("Template IDF file"),
        required=False,
        help_text=_(
            "Upload an IDF file with $VARIABLE_NAME placeholders. "
            "Variables will be auto-detected and shown below."
        ),
    )
    case_sensitive = forms.BooleanField(
        label=_("Case-sensitive variable matching"),
        required=False,
        initial=True,
        help_text=_(
            "When checked, only $UPPERCASE_NAMES are detected as variables. "
            "Uncheck to allow $Mixed_Case names (normalized to uppercase)."
        ),
    )
    remove_template = forms.BooleanField(
        required=False,
        widget=forms.HiddenInput,
    )

    def __init__(self, *args, step=None, org=None, validator=None, **kwargs):
        super().__init__(*args, step=step, org=org, validator=validator, **kwargs)
        self.fields.pop("display_schema", None)

        # Populate weather file choices from ValidatorResourceFile
        self._populate_weather_file_choices(org, validator)

        # ── Template state (for template display in the form) ─────
        # These flags tell the template whether to show "upload" or
        # "current template" UI, and what filename to display.
        from validibot.workflows.models import WorkflowStepResource

        self.has_template = False
        self.template_filename = ""
        self.template_warnings: list[str] = []

        if step:
            config = step.config or {}

            # Read weather file from relational WorkflowStepResource (Phase 0)
            weather_resource = step.step_resources.filter(
                role=WorkflowStepResource.WEATHER_FILE,
            ).first()
            weather_file_id = (
                str(weather_resource.validator_resource_file_id)
                if weather_resource and weather_resource.validator_resource_file_id
                else ""
            )
            self.initial.update(
                {
                    "weather_file": weather_file_id,
                    "idf_checks": config.get("idf_checks", []),
                    "run_simulation": config.get("run_simulation", False),
                    "case_sensitive": config.get("case_sensitive", True),
                    "show_energyplus_warnings": config.get(
                        "show_energyplus_warnings",
                        True,
                    ),
                }
            )
            for key, value in self.initial.items():
                if key in self.fields and value not in (None, ""):
                    self.fields[key].initial = value

            # Check for existing template resource
            template_resource = step.step_resources.filter(
                role=WorkflowStepResource.MODEL_TEMPLATE,
            ).first()
            if template_resource:
                self.has_template = True
                self.template_filename = template_resource.filename or ""

            # Derive initial validation mode from existing step state.
            # If a template resource exists, the step is in template mode.
            initial_mode = (
                self.VALIDATION_MODE_TEMPLATE
                if self.has_template
                else self.VALIDATION_MODE_DIRECT
            )
            self.fields["validation_mode"].initial = initial_mode
        else:
            # Pre-select the first default resource file for new steps
            default_rf = self._get_default_resource_file(org, validator)
            if default_rf:
                self.initial["weather_file"] = str(default_rf.id)

        # ── Crispy Layout ─────────────────────────────────────────
        # Groups fields by validation mode.  Client-side JS toggles
        # the ``d-none`` class on the mode-specific Div wrappers when
        # the author changes the radio selection.  Template variable
        # annotations are now edited via a separate plugin card on
        # the step detail page (see TemplateVariableAnnotationForm).
        self.helper.layout = Layout(
            "name",
            "description",
            "show_success_messages",
            "validation_mode",
            "weather_file",
            "show_energyplus_warnings",
            Div(
                "idf_checks",
                "run_simulation",
                css_class="energyplus-mode-direct",
                data_mode="direct",
            ),
            Div(
                "template_file",
                "case_sensitive",
                "remove_template",
                css_class="energyplus-mode-template",
                data_mode="template",
            ),
            "notes",
        )

    def _populate_weather_file_choices(self, org, validator):
        """Populate weather file dropdown from ValidatorResourceFile."""
        from django.db.models import Q

        from validibot.validations.constants import ResourceFileType
        from validibot.validations.models import ValidatorResourceFile

        choices = [("", _("— Select a weather file —"))]

        if validator:
            # Query resource files: system-wide (org=NULL) or org-specific
            query = Q(org__isnull=True)  # System-wide resources
            if org:
                query |= Q(org=org)  # Plus org-specific resources

            resource_files = (
                ValidatorResourceFile.objects.filter(
                    query,
                    validator=validator,
                    resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
                )
                .select_related("org")
                .order_by("-is_default", "name")
            )

            for rf in resource_files:
                label = rf.name
                if rf.org:
                    label = f"{rf.name} (org)"
                choices.append((str(rf.id), label))

        self.fields["weather_file"].choices = choices
        # Expose to template so we can show a warning when no files are available.
        # len==1 means only the empty placeholder choice was added.
        self.has_weather_files = len(choices) > 1

    def _get_default_resource_file(self, org, validator):
        """Return the first default resource file for pre-selection on new steps."""
        from django.db.models import Q

        from validibot.validations.constants import ResourceFileType
        from validibot.validations.models import ValidatorResourceFile

        if not validator:
            return None

        query = Q(org__isnull=True)
        if org:
            query |= Q(org=org)

        return (
            ValidatorResourceFile.objects.filter(
                query,
                validator=validator,
                resource_type=ResourceFileType.ENERGYPLUS_WEATHER,
                is_default=True,
            )
            .order_by("name")
            .first()
        )


# ---------------------------------------------------------------------------
# Display signals form — used in the modal on the step detail page to
# select which output signals are shown to users in submission results.
# Cross-validator: works for any step type with output signal definitions.
# ---------------------------------------------------------------------------


class DisplaySignalsForm(forms.Form):
    """Form for selecting which output signals appear in submission results.

    Rendered inside a modal on the step detail page.  Populates choices
    from the validator's output signal definitions.  The selection is stored
    in ``step.config["display_signals"]``.
    """

    display_signals = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, step=None, validator=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.step = step
        self.validator = validator

        choices: list[tuple[str, str]] = []
        seen_keys: set[str] = set()

        # Step-owned output signals (FMU outputs, etc.)
        if step:
            from validibot.validations.models import SignalDefinition

            step_outputs = SignalDefinition.objects.filter(
                workflow_step=step,
                direction="output",
            ).order_by("order", "pk")
            for sig in step_outputs:
                key = sig.contract_key
                if key not in seen_keys:
                    seen_keys.add(key)
                    label = sig.label or sig.native_name or sig.contract_key
                    choices.append((key, label))

        # Validator-owned output signals (library catalog)
        if validator:
            from validibot.validations.models import SignalDefinition

            validator_outputs = SignalDefinition.objects.filter(
                validator=validator,
                direction="output",
            ).order_by("order", "pk")
            for sig in validator_outputs:
                key = sig.contract_key
                if key not in seen_keys:
                    seen_keys.add(key)
                    label = sig.label or sig.contract_key
                    choices.append((key, label))

        self.fields["display_signals"].choices = choices

        # Pre-select currently displayed signals
        if step:
            current = (step.config or {}).get("display_signals", [])
            if current:
                self.fields["display_signals"].initial = current


# ---------------------------------------------------------------------------
# Standalone form for editing template variable annotations from the step
# detail page's right-column card.  This form is instantiated by the
# StepEditorCardSpec plugin system — the EnergyPlus ValidatorConfig
# declares it as the form_class for its "template-variables" card.
# ---------------------------------------------------------------------------


def _build_template_vars_from_signals(step: Any) -> list[dict[str, Any]]:
    """Build template variable dicts from step-owned SignalDefinition rows.

    Reads ``SignalDefinition`` rows with ``origin_kind=TEMPLATE`` and their
    ``StepSignalBinding`` to produce dicts that the template variable
    annotation form fields consume.
    """
    if not step or not step.pk:
        return []

    from validibot.validations.constants import SignalOriginKind
    from validibot.validations.models import StepSignalBinding

    bindings = (
        StepSignalBinding.objects.filter(
            workflow_step=step,
            signal_definition__origin_kind=SignalOriginKind.TEMPLATE,
        )
        .select_related("signal_definition")
        .order_by("signal_definition__order", "signal_definition__contract_key")
    )

    result: list[dict[str, Any]] = []
    for binding in bindings:
        sig = binding.signal_definition
        meta = sig.metadata or {}
        default_val = binding.default_value
        result.append(
            {
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
                # Carry the signal PK so we can map back on save.
                "_signal_pk": sig.pk,
                "_binding_pk": binding.pk,
            }
        )
    return result


class TemplateVariableAnnotationForm(forms.Form):
    """Per-variable annotation form for EnergyPlus parameterized templates.

    Rendered in a dedicated card on the step detail page (not inline in
    the step config form).  Accepts a ``step`` kwarg, reads existing
    variable metadata from step-owned ``SignalDefinition`` rows
    (``origin_kind=TEMPLATE``) and their ``StepSignalBinding`` rows.

    The ``template_variable_fields`` property groups bound fields for
    template rendering — the partial iterates over this list to render
    the per-variable annotation cards.
    """

    VARIABLE_TYPE_CHOICES = TEMPLATE_VARIABLE_TYPE_CHOICES

    def __init__(self, *args: Any, step: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._template_variable_meta: list[dict[str, Any]] = []

        template_vars = _build_template_vars_from_signals(step)
        self._create_template_variable_fields(template_vars)

    def _create_template_variable_fields(
        self,
        template_vars: list[dict[str, Any]],
    ) -> None:
        """Create dynamic form fields for each template variable.

        For every variable in ``template_vars``, nine fields are added with
        the naming convention ``tplvar_{index}_{field_name}``.
        """
        for i, var in enumerate(template_vars):
            prefix = f"tplvar_{i}"

            self._template_variable_meta.append(
                {
                    "index": i,
                    "name": var.get("name", ""),
                    "prefix": prefix,
                    "_signal_pk": var.get("_signal_pk"),
                    "_binding_pk": var.get("_binding_pk"),
                }
            )

            self.fields[f"{prefix}_description"] = forms.CharField(
                label=_("Label"),
                max_length=200,
                required=False,
                initial=var.get("description", ""),
                widget=forms.TextInput(
                    attrs={
                        "class": "form-control",
                        "placeholder": _("Human-readable label"),
                    },
                ),
            )
            self.fields[f"{prefix}_default"] = forms.CharField(
                label=_("Default value"),
                max_length=200,
                required=False,
                initial=var.get("default", ""),
                widget=forms.TextInput(
                    attrs={
                        "class": "form-control",
                        "placeholder": _("Leave empty = required"),
                    },
                ),
            )
            self.fields[f"{prefix}_units"] = forms.CharField(
                label=_("Units"),
                max_length=50,
                required=False,
                initial=var.get("units", ""),
                widget=forms.TextInput(
                    attrs={
                        "class": "form-control",
                        "placeholder": _("e.g. W/m2-K"),
                    },
                ),
            )
            self.fields[f"{prefix}_variable_type"] = forms.ChoiceField(
                label=_("Type"),
                choices=self.VARIABLE_TYPE_CHOICES,
                initial=var.get("variable_type", "text"),
                widget=forms.RadioSelect(
                    attrs={"class": "form-check-input"},
                ),
            )
            min_val = var.get("min_value")
            self.fields[f"{prefix}_min_value"] = forms.CharField(
                label=_("Min value"),
                required=False,
                initial=str(min_val) if min_val is not None else "",
                widget=forms.TextInput(
                    attrs={"class": "form-control", "placeholder": _("—")},
                ),
            )
            self.fields[f"{prefix}_min_exclusive"] = forms.BooleanField(
                label=_("Exclusive"),
                required=False,
                initial=var.get("min_exclusive", False),
                widget=forms.CheckboxInput(
                    attrs={"class": "form-check-input"},
                ),
            )
            max_val = var.get("max_value")
            self.fields[f"{prefix}_max_value"] = forms.CharField(
                label=_("Max value"),
                required=False,
                initial=str(max_val) if max_val is not None else "",
                widget=forms.TextInput(
                    attrs={"class": "form-control", "placeholder": _("—")},
                ),
            )
            self.fields[f"{prefix}_max_exclusive"] = forms.BooleanField(
                label=_("Exclusive"),
                required=False,
                initial=var.get("max_exclusive", False),
                widget=forms.CheckboxInput(
                    attrs={"class": "form-check-input"},
                ),
            )
            choices_list = var.get("choices", [])
            self.fields[f"{prefix}_choices"] = forms.CharField(
                label=_("Allowed values"),
                required=False,
                initial="\n".join(choices_list),
                widget=forms.Textarea(
                    attrs={
                        "class": "form-control",
                        "rows": 4,
                        "placeholder": _("Enter one value per line"),
                    },
                ),
            )

    @property
    def template_variable_fields(self) -> list[dict[str, Any]]:
        """Return template variable fields grouped for template rendering.

        Each item contains the variable's name, index, and BoundField
        objects keyed by field name.
        """
        result: list[dict[str, Any]] = []
        for meta in self._template_variable_meta:
            prefix = meta["prefix"]
            default_val = self[f"{prefix}_default"].value() or ""
            result.append(
                {
                    "index": meta["index"],
                    "name": meta["name"],
                    "is_required": not bool(default_val),
                    "description": self[f"{prefix}_description"],
                    "default": self[f"{prefix}_default"],
                    "units": self[f"{prefix}_units"],
                    "variable_type": self[f"{prefix}_variable_type"],
                    "min_value": self[f"{prefix}_min_value"],
                    "min_exclusive": self[f"{prefix}_min_exclusive"],
                    "max_value": self[f"{prefix}_max_value"],
                    "max_exclusive": self[f"{prefix}_max_exclusive"],
                    "choices": self[f"{prefix}_choices"],
                }
            )
        return result


class SingleTemplateVariableForm(forms.Form):
    """Form for editing a single template variable's annotations via modal.

    Unlike ``TemplateVariableAnnotationForm`` which creates dynamic fields
    for all variables at once, this form handles one variable at a time.
    Used by the per-variable edit modal in the unified signals card.

    See ADR-2026-03-10: Unified Input/Output Signals UI.
    """

    description = forms.CharField(
        label=_("Label"),
        max_length=200,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": _("Human-readable label"),
            },
        ),
    )
    default = forms.CharField(
        label=_("Default value"),
        max_length=200,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": _("Leave empty = required"),
            },
        ),
    )
    units = forms.CharField(
        label=_("Units"),
        max_length=50,
        required=False,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": _("e.g. W/m2-K"),
            },
        ),
    )
    variable_type = forms.ChoiceField(
        label=_("Type"),
        choices=TEMPLATE_VARIABLE_TYPE_CHOICES,
        initial="text",
        widget=forms.RadioSelect(
            attrs={"class": "form-check-input"},
        ),
    )
    min_value = forms.CharField(
        label=_("Min value"),
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": _("—")},
        ),
    )
    min_exclusive = forms.BooleanField(
        label=_("Exclusive"),
        required=False,
        widget=forms.CheckboxInput(
            attrs={"class": "form-check-input"},
        ),
    )
    max_value = forms.CharField(
        label=_("Max value"),
        required=False,
        widget=forms.TextInput(
            attrs={"class": "form-control", "placeholder": _("—")},
        ),
    )
    max_exclusive = forms.BooleanField(
        label=_("Exclusive"),
        required=False,
        widget=forms.CheckboxInput(
            attrs={"class": "form-check-input"},
        ),
    )
    choices = forms.CharField(
        label=_("Allowed values"),
        required=False,
        widget=forms.Textarea(
            attrs={
                "class": "form-control",
                "rows": 4,
                "placeholder": _("Enter one value per line"),
            },
        ),
    )

    def __init__(self, *args: Any, variable: dict | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if variable:
            self.fields["description"].initial = variable.get("description", "")
            self.fields["default"].initial = variable.get("default", "")
            self.fields["units"].initial = variable.get("units", "")
            self.fields["variable_type"].initial = variable.get("variable_type", "text")
            min_val = variable.get("min_value")
            self.fields["min_value"].initial = (
                str(min_val) if min_val is not None else ""
            )
            self.fields["min_exclusive"].initial = variable.get("min_exclusive", False)
            max_val = variable.get("max_value")
            self.fields["max_value"].initial = (
                str(max_val) if max_val is not None else ""
            )
            self.fields["max_exclusive"].initial = variable.get("max_exclusive", False)
            choices_list = variable.get("choices", [])
            self.fields["choices"].initial = "\n".join(choices_list)


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
        ValidationType.FMU: FMUValidatorStepConfigForm,
        ValidationType.AI_ASSIST: AiAssistStepConfigForm,
    }
    return mapping.get(validation_type, BaseStepConfigForm)


class WorkflowPublicInfoForm(forms.ModelForm):
    make_info_page_public = forms.BooleanField(
        label=_("Make info page public"),
        required=False,
        help_text=_(
            "When enabled, anyone with the link can view the workflow's info page.",
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
        self.fields["make_info_page_public"].initial = bool(
            workflow.make_info_page_public,
        )
        self.fields["make_info_page_public"].widget.attrs.setdefault(
            "class",
            "form-check-input",
        )
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Field("title"),
            Field("content_md"),
            Field("make_info_page_public"),
        )


class WorkflowBreakGlassDeleteForm(forms.Form):
    """Collect explicit confirmation before tombstoning a workflow.

    The break-glass flow is intentionally heavier than ordinary archive/delete.
    The operator must confirm the immutable workflow UUID, record a human
    reason, and acknowledge the impact on normal product surfaces before the
    workflow is tombstoned.
    """

    workflow_uuid_confirmation = forms.CharField(
        label=_("Type the workflow UUID to continue"),
        help_text=_(
            "This confirmation uses the immutable workflow UUID, not the "
            "workflow name or slug."
        ),
    )
    deletion_reason = forms.CharField(
        label=_("Reason for break-glass delete"),
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=_(
            "Explain why the workflow must be removed from normal product surfaces."
        ),
    )
    acknowledge_consequences = forms.BooleanField(
        label=_(
            "I understand that this workflow will stop appearing in normal "
            "lists, launch flows, and editing screens, while historical runs "
            "and credentials remain valid."
        ),
        required=True,
    )

    def __init__(self, *args, workflow: Workflow, **kwargs):
        self.workflow = workflow
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            HTML(
                '<div class="alert alert-warning small mb-3">'
                + str(
                    _(
                        "Break-glass delete is an exceptional workflow "
                        "lifecycle action for credential-bearing workflows."
                    ),
                )
                + "</div>",
            ),
            Field("workflow_uuid_confirmation"),
            Field("deletion_reason"),
            Field("acknowledge_consequences"),
        )

    def clean_workflow_uuid_confirmation(self) -> str:
        """Require an exact UUID match before allowing tombstoning."""
        value = (self.cleaned_data.get("workflow_uuid_confirmation") or "").strip()
        expected = str(self.workflow.uuid)
        if value != expected:
            raise ValidationError(
                _("Enter the exact workflow UUID: %(uuid)s") % {"uuid": expected},
            )
        return value


class BasicStepConfigForm(BaseStepConfigForm):
    """Minimal form for manual assertion steps (name/description/notes only)."""

    def __init__(self, *args, step=None, **kwargs):
        super().__init__(*args, step=step, **kwargs)
        self.fields.pop("display_schema", None)


class SignalBindingEditForm(forms.Form):
    """Edit form for signal definition and binding fields.

    Supports editing both ``SignalDefinition`` metadata (label, description,
    unit) and ``StepSignalBinding`` configuration (source_data_path,
    default_value, is_required). For library-owned signals, definition
    fields are rendered as read-only; for step-owned signals, all fields
    are editable.

    See ADR-2026-03-18, Phase 5, Item 28.
    """

    # Definition fields (read-only for library signals)
    label = forms.CharField(
        max_length=255,
        required=False,
        label=_("Label"),
        help_text=_("Human-readable display name for this signal."),
    )
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 2}),
        label=_("Description"),
    )
    unit = forms.CharField(
        max_length=50,
        required=False,
        label=_("Unit"),
        help_text=_("Unit of measurement (e.g., kW, m², °C)."),
    )

    # Binding fields (always editable when binding exists)
    source_data_path = forms.CharField(
        max_length=500,
        required=False,
        label=_("Source Path"),
        help_text=_(
            "A payload path (e.g., building.floor_area) or a signal "
            "reference (e.g., s.floor_area) that provides the value "
            "for this input."
        ),
    )
    default_value = forms.CharField(
        required=False,
        label=_("Default Value"),
        help_text=_(
            "Fallback value when the source path resolves to nothing. "
            "Leave empty to make the signal required."
        ),
    )
    is_required = forms.BooleanField(
        required=False,
        label=_("Required"),
        help_text=_(
            "If checked, validation fails when this signal is missing. "
            "Cannot be used together with a default value."
        ),
    )

    def __init__(self, *args, signal_definition=None, binding=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.signal_definition = signal_definition
        self.binding = binding

        # Pre-populate from existing data.
        if signal_definition and not self.is_bound:
            self.fields["label"].initial = signal_definition.label
            self.fields["description"].initial = signal_definition.description
            self.fields["unit"].initial = signal_definition.unit

        if binding and not self.is_bound:
            self.fields["source_data_path"].initial = binding.source_data_path
            if binding.default_value is not None:
                self.fields["default_value"].initial = str(binding.default_value)
            self.fields["is_required"].initial = binding.is_required

        # Library-owned signals: definition fields are read-only.
        if signal_definition and signal_definition.validator_id:
            for field_name in ("label", "description", "unit"):
                self.fields[field_name].disabled = True

    def clean(self):
        cleaned = super().clean()
        default_value = (cleaned.get("default_value") or "").strip()
        is_required = cleaned.get("is_required", False)
        if default_value and is_required:
            raise forms.ValidationError(
                _(
                    "A signal cannot be both required and have a default "
                    "value. Either remove the default or uncheck Required."
                ),
            )
        return cleaned

    def save(self):
        """Persist changes to the signal definition and/or binding."""
        sig = self.signal_definition
        binding = self.binding

        if sig and not sig.validator_id:
            # Step-owned signal: update definition fields.
            sig.label = self.cleaned_data.get("label") or ""
            sig.description = self.cleaned_data.get("description") or ""
            sig.unit = self.cleaned_data.get("unit") or ""
            sig.save(update_fields=["label", "description", "unit"])

        if binding:
            binding.source_data_path = self.cleaned_data.get("source_data_path") or ""
            default_str = self.cleaned_data.get("default_value", "").strip()
            binding.default_value = default_str if default_str else None
            binding.is_required = self.cleaned_data.get("is_required", True)
            binding.save(
                update_fields=["source_data_path", "default_value", "is_required"],
            )


# ── Workflow Signal Mapping ───────────────────────────────────────────
# Form for the add/edit modal in the signal mapping editor page.
# Each mapping defines a named signal (s.<name>) that resolves a data
# path in the submission payload before any validation step runs.

ON_MISSING_CHOICES = (
    ("error", _("Error — fail the run")),
    ("null", _("Null — inject null")),
)

DATA_TYPE_CHOICES = (
    ("", _("Auto (infer from data)")),
    ("number", _("Number")),
    ("string", _("String")),
    ("boolean", _("Boolean")),
    ("object", _("Object")),
    ("array", _("Array")),
)


class WorkflowSignalMappingForm(forms.Form):
    """Form for creating and editing workflow-level signal mappings.

    Signal mappings define author-named signals (``s.<name>``) that
    extract values from submission data paths.  This form handles
    validation of the signal name (must be a valid CEL identifier, not
    a reserved namespace, and unique within the workflow) and the
    optional default value (must be valid JSON if provided).
    """

    name = forms.CharField(
        max_length=100,
        label=_("Signal name"),
        help_text=_("Used in CEL expressions as s.name."),
    )
    source_path = forms.CharField(
        max_length=500,
        label=_("Source path"),
        help_text=_(
            "Data path in the submission payload (e.g. materials[0].emissivity)."
        ),
    )
    on_missing = forms.ChoiceField(
        choices=ON_MISSING_CHOICES,
        initial="error",
        label=_("On missing"),
        help_text=_("What happens when the source path cannot be resolved."),
    )
    default_value = forms.CharField(
        required=False,
        label=_("Default value"),
        help_text=_('Fallback value as JSON (e.g. 0, "none", null).'),
    )
    data_type = forms.ChoiceField(
        choices=DATA_TYPE_CHOICES,
        required=False,
        initial="",
        label=_("Data type"),
        help_text=_("Expected type. Leave as Auto to infer from data."),
    )

    def __init__(
        self,
        *args,
        workflow: Workflow | None = None,
        exclude_mapping_id: int | None = None,
        **kwargs,
    ):
        self.workflow = workflow
        self.exclude_mapping_id = exclude_mapping_id
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column("name", css_class="col-12 col-lg-6"),
                Column("data_type", css_class="col-12 col-lg-6"),
            ),
            "source_path",
            Row(
                Column("on_missing", css_class="col-12 col-lg-6"),
                Column("default_value", css_class="col-12 col-lg-6"),
            ),
        )

    def clean_name(self) -> str:
        """Validate signal name: CEL identifier, not reserved, unique."""
        from validibot.validations.services.signal_resolution import (
            validate_signal_name,
        )
        from validibot.validations.services.signal_resolution import (
            validate_signal_name_unique,
        )

        name = self.cleaned_data["name"].strip()

        errors = validate_signal_name(name)
        if errors:
            raise ValidationError(errors)

        if self.workflow:
            unique_errors = validate_signal_name_unique(
                workflow_id=self.workflow.pk,
                name=name,
                exclude_mapping_id=self.exclude_mapping_id,
            )
            if unique_errors:
                raise ValidationError(unique_errors)

        return name

    def clean_default_value(self) -> str:
        """Validate that default_value is valid JSON if provided."""
        raw = self.cleaned_data.get("default_value", "").strip()
        if not raw:
            return ""
        try:
            json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                _('Default value must be valid JSON (e.g. 42, "hello", null).'),
            ) from exc
        return raw

    def save_mapping(
        self,
        workflow: Workflow,
        *,
        instance: WorkflowSignalMapping | None = None,
    ) -> WorkflowSignalMapping:
        """Create or update a WorkflowSignalMapping from cleaned data.

        When creating, auto-assigns the next position value so the new
        mapping appears at the end of the list.
        """
        default_str = self.cleaned_data["default_value"]
        default_value = json.loads(default_str) if default_str else None

        if instance:
            instance.name = self.cleaned_data["name"]
            instance.source_path = self.cleaned_data["source_path"]
            instance.on_missing = self.cleaned_data["on_missing"]
            instance.default_value = default_value
            instance.data_type = self.cleaned_data["data_type"]
            instance.save()
            return instance

        # New mapping: assign position after the last existing mapping
        last_position = (
            WorkflowSignalMapping.objects.filter(workflow=workflow)
            .order_by("-position")
            .values_list("position", flat=True)
            .first()
        )
        next_position = (last_position or 0) + 10

        return WorkflowSignalMapping.objects.create(
            workflow=workflow,
            name=self.cleaned_data["name"],
            source_path=self.cleaned_data["source_path"],
            on_missing=self.cleaned_data["on_missing"],
            default_value=default_value,
            data_type=self.cleaned_data["data_type"],
            position=next_position,
        )
