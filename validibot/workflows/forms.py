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
from django.template.defaultfilters import filesizeformat
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import format_html
from django.utils.html import format_html_join
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from validibot.projects.models import Project
from validibot.submissions.constants import SubmissionFileType
from validibot.validations.cel_columns import referenced_column_aggregates
from validibot.validations.cel_columns import referenced_column_metrics
from validibot.validations.cel_columns import referenced_row_columns
from validibot.validations.constants import VALIDATION_RUN_SHORT_DESCRIPTION_MAX_LENGTH
from validibot.validations.constants import JSONSchemaVersion
from validibot.validations.constants import ValidationType
from validibot.validations.constants import XMLSchemaType
from validibot.validations.regex_safety import UnsafeOrInvalidPatternError
from validibot.validations.regex_safety import compile_user_pattern
from validibot.validations.validators.shacl.constants import (
    SHACL_RESULT_FAIL_AFTER_ASSERTIONS,
)
from validibot.validations.validators.shacl.constants import (
    SHACL_RESULT_FAIL_IMMEDIATELY,
)
from validibot.validations.validators.shacl.constants import (
    SHACL_RESULT_HANDLING_DEFAULT,
)
from validibot.validations.validators.shacl.constants import SHACL_RESULT_REPORT_ONLY

# SHACL form pieces (field declarations, multi-file widget, size caps,
# clean helpers) live under the validator's own package so the library-
# validator forms in validibot.validations.forms can use the same
# mixin. Imported here for the workflow step config form below; the
# SHACL_PER_FILE_MAX_BYTES re-export keeps existing form tests that
# import the constant from this module working unchanged.
from validibot.validations.validators.shacl.form_fields import SHACL_PER_FILE_MAX_BYTES
from validibot.validations.validators.shacl.form_fields import ShaclConfigMixin
from validibot.workflows.constants import WorkflowHistoryPolicy
from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowPublicInfo
from validibot.workflows.models import WorkflowSignalMapping

if TYPE_CHECKING:
    from validibot.users.models import User

logger = logging.getLogger(__name__)


def _membership_for_access_gate(user, org):
    """Return active membership for access-policy editing, if any."""
    if not user or not getattr(user, "is_authenticated", False) or org is None:
        return None
    from validibot.users.models import Membership

    return (
        Membership.objects.filter(user=user, org=org, is_active=True)
        .prefetch_related("membership_roles__role")
        .first()
    )


def _membership_is_org_admin(membership) -> bool:
    """Return True for org owner/admin roles, without Django superuser bypass."""
    if membership is None:
        return False
    from validibot.users.constants import RoleCode

    return membership.has_role(RoleCode.OWNER) or membership.has_role(RoleCode.ADMIN)


def _membership_is_author(membership) -> bool:
    """Return True when membership carries workflow-author capability."""
    if membership is None:
        return False
    from validibot.users.constants import RoleCode

    return (
        membership.has_role(RoleCode.OWNER)
        or membership.has_role(RoleCode.ADMIN)
        or membership.has_role(RoleCode.AUTHOR)
    )


class AllowedFileTypesCheckboxSelectMultiple(forms.CheckboxSelectMultiple):
    """Render workflow file-type choices with aligned extension hints."""

    option_template_name = "workflows/widgets/allowed_file_type_option.html"


@dataclass(frozen=True)
class FileTypeChoiceLabel:
    """Display label plus extension hint for the workflow file-type picker."""

    name: Any
    examples: Any

    def __str__(self) -> str:
        return str(self.name)


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
APP_FORM_SECTION_CLASS = "app-form-section p-3 mb-4"
APP_FORM_SUBSECTION_CLASS = "app-form-section p-3 mb-3"


# SHACL_PER_FILE_MAX_BYTES + ShaclConfigMixin are imported above; the
# names re-exported via __all__ so existing form tests that import them
# from this module continue to resolve.
__all__ = [
    "SHACL_PER_FILE_MAX_BYTES",
    "ShaclConfigMixin",
]


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


def form_section_intro(title: str, body: str) -> HTML:
    """Render the standard intro block for grouped app form sections."""
    return HTML(
        '<div class="mb-3">'
        f'<h6 class="mb-1">{title}</h6>'
        f'<p class="text-muted small mb-0">{body}</p>'
        "</div>",
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
                        _("Operator '%(op)s' requires a comparison value.")
                        % {"op": operator},
                    )
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
            "input_retention",
            "output_retention",
            "success_message",
            "allow_submission_name",
            "allow_submission_meta_data",
            "allow_submission_short_description",
            "featured_image",
            "version",
            "history_policy",
            "is_active",
        ]
        help_texts = {
            "version": _(
                "Required number to track workflow iterations. Use a positive "
                "integer (e.g. 1). Defaults to 1 for a brand-new workflow.",
            ),
            "is_active": _(
                "Disable a workflow to pause new validation runs without removing it.",
            ),
            "allowed_file_types": _(
                "Choose the submission file types this workflow accepts. "
                "Launchers can only upload/run content using these formats."
            ),
            "history_policy": _(
                "Versioned history preserves reproducibility by requiring a "
                "new workflow version for semantic edits after runs exist. "
                "Mutable history permits in-place edits after runs, but old "
                "run results may no longer match the current workflow definition."
            ),
            "input_retention": _(
                "Controls how long the user's submission data is kept after "
                "validation. The submission record is always preserved for "
                "audit purposes."
            ),
            "output_retention": _(
                "Controls how long validation outputs (results, artifacts, "
                "findings) are kept after the run completes. Users need time "
                "to review results, so immediate deletion is not an option."
            ),
        }

    def __init__(
        self,
        *args,
        user=None,
        enforce_history_lock: bool = True,
        **kwargs,
    ):
        self.user = user
        self.enforce_history_lock = enforce_history_lock
        self.requires_new_version_for_save = False
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
        history_field = self.fields["history_policy"]
        history_field.label = _("History policy")
        history_field.required = False
        history_field.widget.attrs.update({"class": "form-select"})
        history_field.initial = (
            self.instance.history_policy
            if self.instance and self.instance.pk
            else WorkflowHistoryPolicy.VERSIONED
        )

        # Surface the lock state in the UI instead of letting the user
        # edit a field that the server will reject on submit. Disabling
        # the field also makes Django's form layer ignore any submitted
        # value, so the existing server-side check in
        # ``_clean_history_policy_lock`` remains a defence-in-depth
        # guard rather than the sole gate.
        history_is_locked = (
            self.instance
            and self.instance.pk
            and self.enforce_history_lock
            and (self.instance.is_locked or self.instance.has_runs())
        )
        is_superuser = bool(
            self.user and getattr(self.user, "is_superuser", False),
        )
        if history_is_locked and not is_superuser:
            history_field.disabled = True
            if self.instance.is_locked:
                lock_reason = _(
                    "This workflow is locked, so its history policy is "
                    "fixed. To change it, create a new workflow version."
                )
            else:
                lock_reason = _(
                    "This workflow already has validation runs, so its "
                    "history policy is fixed to keep past runs tied to "
                    "the definition that produced them. To change it, "
                    "create a new workflow version."
                )
            history_field.help_text = lock_reason
        else:
            history_field.help_text = _(
                "Choose how this workflow treats edits after validation "
                "runs exist. Versioned history is recommended: semantic "
                "edits create a new workflow version so old runs remain "
                "tied to the definition that produced them. Mutable "
                "history allows in-place edits for faster iteration, "
                "but historical run results may not be reproducible "
                "against the current workflow definition. After runs "
                "exist, change history policy by creating a new "
                "workflow version."
            )
        self.fields["version"].widget.attrs.update(
            {
                "class": "form-control",
                "min": "1",
                "step": "1",
            },
        )
        allowed_field = self.fields["allowed_file_types"]
        # Override the enum's bare labels with extension hints so authors
        # don't have to guess which broad category covers (say) ``.ttl``.
        # The enum labels stay clean in admin / API / audit surfaces;
        # only this form sees structured labels with muted extension hints.
        allowed_field.widget = AllowedFileTypesCheckboxSelectMultiple()
        allowed_field.choices = [
            (
                SubmissionFileType.JSON.value,
                FileTypeChoiceLabel(_("JSON"), _(".json, .jsonld")),
            ),
            (
                SubmissionFileType.XML.value,
                FileTypeChoiceLabel(_("XML"), _(".xml, .rdf, .xsd")),
            ),
            (
                SubmissionFileType.TEXT.value,
                FileTypeChoiceLabel(
                    _("Plain Text"),
                    _(".txt, .csv, .ttl, .nt, .nq"),
                ),
            ),
            (
                SubmissionFileType.YAML.value,
                FileTypeChoiceLabel(_("YAML"), _(".yml, .yaml")),
            ),
            (
                SubmissionFileType.BINARY.value,
                FileTypeChoiceLabel(_("Binary"), _(".fmu, .zip")),
            ),
            (
                SubmissionFileType.UNKNOWN.value,
                FileTypeChoiceLabel(_("Unknown"), _("any extension")),
            ),
        ]
        # Surface the layering relationship in the help text so authors
        # know the validator step does its own extension check on top.
        allowed_field.help_text = _(
            "Choose the submission file types this workflow accepts. "
            "Each validator in the workflow may further restrict to its "
            "specific format — for example, the SHACL validator only "
            "accepts .ttl, .rdf, .jsonld, .nt, and .nq regardless of "
            "which broad types you allow here.",
        )
        if self.instance and self.instance.pk:
            allowed_field.initial = list(self.instance.allowed_file_types or [])
        elif not allowed_field.initial:
            allowed_field.initial = [SubmissionFileType.JSON]
        # Configure input retention field
        self.fields["input_retention"].label = _("Input retention")
        self.fields["input_retention"].widget.attrs.update({"class": "form-select"})
        # Configure output retention field (parallel to input_retention)
        self.fields["output_retention"].label = _("Output retention")
        self.fields["output_retention"].widget.attrs.update({"class": "form-select"})
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

        # ── Access controls (visibility + agent channels) ───────────────
        # These fields are not in Meta.fields — they are added dynamically
        # and only when the current user is allowed to adjust access (see
        # ``_user_can_edit_access``). For everyone else the access section
        # is omitted entirely, so a submitted value can never change the
        # workflow's audience.
        self._access_fields: list[str] = []
        if self._user_can_edit_access():
            self._add_access_fields()

        self.helper.layout = self._build_layout()

    def _user_can_edit_access(self) -> bool:
        """Return True when the current user may edit the access controls.

        Access controls (visibility, MCP, x402, billing/price/launches)
        are privileged: changing who can run a workflow — or publishing it
        for paid anonymous access — has security and billing consequences.
        They are therefore editable only when the user has an org admin
        role in the workflow's organization, OR the organization has opted
        in via ``Organization.allow_authors_to_adjust_access`` for authors.

        The org-level ceilings themselves (visibility cap, mcp/x402
        allowed) are NEVER exposed in this form — they are an admin-only
        concern. This gate only decides whether the per-workflow controls
        appear at all.
        """
        user = self.user
        org = self._org_for_access()
        membership = _membership_for_access_gate(user, org)
        if _membership_is_org_admin(membership):
            return True
        if not (org and getattr(org, "allow_authors_to_adjust_access", False)):
            return False
        if not _membership_is_author(membership):
            return False
        if self.instance and self.instance.pk:
            return self.instance.user_id == getattr(user, "id", None)
        return True

    def _org_for_access(self):
        """Return the org whose ceilings clamp this workflow's access.

        Prefers the bound instance's org (edit flow); falls back to the
        user's current org (create flow) so a brand-new workflow still
        clamps its visibility choices to the right ceiling.
        """
        org = getattr(self.instance, "org", None) if self.instance else None
        if org is not None:
            return org
        if self.user and getattr(self.user, "is_authenticated", False):
            return self.user.get_current_org()
        return None

    def _allowed_visibility_choices(self):
        """Return visibility choices clamped to the org's ceiling.

        Uses the central ``WORKFLOW_VISIBILITY_ORDER`` /
        ``visibility_within_cap`` definition of "no wider than" so the
        form, model, and resolvers all agree on what the cap means. When
        no org/cap is resolvable we fall back to the full set (the model
        and view still re-clamp, so this is display-only).
        """
        from validibot.workflows.constants import WORKFLOW_VISIBILITY_ORDER
        from validibot.workflows.constants import WorkflowVisibility
        from validibot.workflows.constants import visibility_within_cap

        org = self._org_for_access()
        cap = getattr(org, "workflow_visibility_cap", None) or (
            WorkflowVisibility.ALL_USERS
        )
        labels = dict(WorkflowVisibility.choices)
        return [
            (value, labels[value])
            for value in WORKFLOW_VISIBILITY_ORDER
            if visibility_within_cap(value, cap)
        ]

    def _add_access_fields(self) -> None:
        """Add the per-workflow access controls to the form.

        Adds the identity-scoped ``workflow_visibility`` selector plus the
        two independent agent channels (``mcp_enabled``, ``x402_enabled``)
        and their billing/price/rate-limit companions. Each control is
        clamped to the organization's ceiling: visibility choices are
        capped, and the MCP / x402 toggles are disabled (with an
        explanatory note) when the org has not allowed that channel.
        """
        from validibot.workflows.constants import AgentBillingMode
        from validibot.workflows.constants import WorkflowVisibility

        org = self._org_for_access()
        mcp_allowed = bool(getattr(org, "mcp_allowed", False))
        x402_allowed = bool(getattr(org, "x402_allowed", False))

        # ── WHO (identity-scoped visibility) ──────────────────────────
        self.fields["workflow_visibility"] = forms.ChoiceField(
            choices=self._allowed_visibility_choices(),
            label=_("Who can run this workflow"),
            # Not hard-required: a submission that omits the tier (a
            # non-browser client, or a minimal create POST) must fall back
            # to the secure default in ``clean()`` rather than 400-ing.
            # The model field defaults to PRIVATE, so omitting it can only
            # narrow access, never widen it.
            required=False,
            help_text=_(
                "Who, by Validibot identity, may run this workflow for "
                "free. Limited to your organization's ceiling. This is "
                "independent of paid anonymous (x402) access below.",
            ),
            widget=forms.Select(attrs={"class": "form-select"}),
        )
        self.fields["workflow_visibility"].initial = (
            self.instance.workflow_visibility
            if self.instance and self.instance.pk
            else WorkflowVisibility.PRIVATE
        )
        self._access_fields.append("workflow_visibility")

        # ── HOW: MCP (authenticated agents on behalf of a user) ───────
        mcp_help = _(
            "Allow authenticated AI agents to run this workflow via MCP, "
            "on behalf of a user who already has identity access above. "
            "Billed to that user's plan quota.",
        )
        if not mcp_allowed:
            mcp_help = _("MCP access is disabled for this organization.")
        self.fields["mcp_enabled"] = forms.BooleanField(
            required=False,
            label=_("Allow authenticated agents via MCP"),
            help_text=mcp_help,
            disabled=not mcp_allowed,
        )
        if self.instance and self.instance.pk:
            self.fields["mcp_enabled"].initial = self.instance.mcp_enabled
        self._access_fields.append("mcp_enabled")

        # ── HOW: x402 (paid anonymous access to anyone on the internet)
        # DANGER ZONE: this is independent of visibility and exposes the
        # workflow to the public internet for pay-per-call. The note below
        # is shown wherever this control appears (here and in the template).
        x402_help = _(
            "Enabling this makes the workflow callable by ANYONE on the "
            "internet who pays the per-call price (anonymous x402), "
            "regardless of the visibility setting above.",
        )
        if not x402_allowed:
            x402_help = _("x402 access is disabled for this organization.")
        self.fields["x402_enabled"] = forms.BooleanField(
            required=False,
            label=_("Publish for paid anonymous access (x402)"),
            help_text=x402_help,
            disabled=not x402_allowed,
        )
        if self.instance and self.instance.pk:
            self.fields["x402_enabled"].initial = self.instance.x402_enabled
        self._access_fields.append("x402_enabled")

        self.fields["agent_billing_mode"] = forms.ChoiceField(
            choices=AgentBillingMode.choices,
            initial=AgentBillingMode.AUTHOR_PAYS,
            # Not hard-required: a submission that omits it falls back to
            # the model default (AUTHOR_PAYS) in ``clean()``. Enabling x402
            # re-selects the x402 rail via the model cascade regardless.
            required=False,
            label=_("Agent billing mode"),
            help_text=_(
                "Who pays when an agent invokes this workflow.",
            ),
            widget=forms.Select(attrs={"class": "form-select"}),
        )
        if self.instance and self.instance.pk:
            self.fields["agent_billing_mode"].initial = self.instance.agent_billing_mode
        self._access_fields.append("agent_billing_mode")

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
        self._access_fields.append("agent_price_cents")

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
        self._access_fields.append("agent_max_launches_per_hour")

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
                css_class=APP_FORM_SECTION_CLASS,
            ),
            Div(
                self._section_intro(
                    _("Submission settings"),
                    _(
                        "Define which file types the workflow accepts and how "
                        "submission metadata should behave."
                    ),
                ),
                Field(
                    "allowed_file_types",
                    template="workflows/fields/allowed_file_types.html",
                ),
                Field("input_retention"),
                Field("output_retention"),
                Field("success_message"),
                Field("allow_submission_name"),
                Field("allow_submission_meta_data"),
                Field("allow_submission_short_description"),
                css_class=APP_FORM_SECTION_CLASS,
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
                    css_class=APP_FORM_SUBSECTION_CLASS,
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
                    css_class=APP_FORM_SUBSECTION_CLASS,
                    data_input_schema_mode_value="pydantic",
                ),
                # Hidden model fields — populated by clean()
                Field("input_schema", type="hidden"),
                Field("input_schema_source_mode", type="hidden"),
                Field("input_schema_source_text", type="hidden"),
                css_class=APP_FORM_SECTION_CLASS,
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
                Row(
                    Column(
                        Field("version", placeholder="e.g. 1"),
                        css_class="col-12 col-md-4 col-xl-3",
                    ),
                    Column(
                        Field("history_policy"),
                        css_class="col-12 col-md-5 col-xl-4",
                    ),
                    css_class="g-3",
                ),
                Field("is_active"),
                css_class=APP_FORM_SECTION_CLASS,
            ),
        ]

        if self._user_can_edit_access():
            # DANGER-ZONE note for x402: rendered alongside the x402 toggle
            # so authors cannot miss that enabling it exposes the workflow
            # to anyone on the public internet who pays, independent of the
            # identity-scoped visibility above.
            x402_danger_note = HTML(
                '<div class="alert alert-warning small mb-3" role="alert">'
                '<i class="bi-exclamation-triangle me-1"></i>'
                f"{
                    _(
                        'Enabling paid anonymous access (x402) makes the '
                        'workflow callable by ANYONE on the internet who pays '
                        'the per-call price, regardless of the visibility '
                        'setting above.'
                    )
                }"
                "</div>",
            )
            sections.append(
                Div(
                    self._section_intro(
                        _("Access"),
                        _(
                            "Choose who can run this workflow by Validibot "
                            "identity, and whether AI agents may invoke it. "
                            "Options are limited by your organization's "
                            "ceilings."
                        ),
                    ),
                    Field("workflow_visibility"),
                    Field("mcp_enabled"),
                    Field("x402_enabled"),
                    x402_danger_note,
                    Field("agent_billing_mode"),
                    Field("agent_price_cents"),
                    Field("agent_max_launches_per_hour"),
                    css_class=APP_FORM_SECTION_CLASS,
                ),
            )

        return Layout(*sections)

    def _section_intro(self, title: str, body: str) -> HTML:
        """Render a compact section heading for the crispy form layout."""
        return form_section_intro(title, body)

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise ValidationError(_("Name is required."))
        return name

    def clean_project(self):
        project = self.cleaned_data.get("project")
        if project is None:
            # ``required=True`` already raises for an empty selection, but guard
            # explicitly so any caller that bypasses field-level validation
            # still gets a clear, field-scoped error rather than a later
            # model-level failure.
            raise ValidationError(_("A workflow must belong to a project."))

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

    @staticmethod
    def _contract_lock_message(
        *,
        field_name: str,
        current: Any,
        proposed: Any,
    ) -> str:
        """Build a per-field, direction-aware error message for the gate.

        Generic "cannot change in place" doesn't tell the author what
        specifically broke the safety check. This helper produces a
        message that names the field, identifies whether they tried to
        narrow or shorten, and points at the safe-change escape hatch
        (add types in place; create a new version only to remove them).
        """
        if field_name == "allowed_file_types":
            current_set = set(current or [])
            proposed_set = set(proposed or [])
            removed = current_set - proposed_set
            if removed:
                removed_labels = ", ".join(sorted(removed))
                return _(
                    "Removing %(removed)s from allowed file types would "
                    "invalidate past runs that accepted those types. You "
                    "can add new types in place — to remove one, create "
                    "a new version of the workflow.",
                ) % {"removed": removed_labels}
        if field_name in {"input_retention", "output_retention"}:
            return _(
                "Shortening %(field)s would purge data sooner than past "
                "runs were promised. You can extend retention in place — "
                "to shorten it, create a new version of the workflow.",
            ) % {"field": field_name.replace("_", " ")}
        return _(
            "Changing %(field)s on a workflow that already has runs would "
            "invalidate them. Create a new version of the workflow to "
            "make this change.",
        ) % {"field": field_name}

    def clean(self):
        """Run the input-contract authoring pipeline.

        If the author provided input contract text in either mode, this
        method converts it to canonical JSON Schema, validates the supported
        v1 subset, and writes the result into the hidden model fields.

        Also enforces cross-field invariants on the author's privacy
        choices (e.g., x402 billing requires DO_NOT_STORE retention).
        """
        from validibot.submissions.constants import SubmissionRetention
        from validibot.workflows.constants import AgentBillingMode

        cleaned = super().clean()

        # ── Cascade: enabling x402 selects the x402 billing rail ──────
        # x402 and MCP are INDEPENDENT channels (no cascade between them).
        # Publishing for paid anonymous access implies agents-pay-x402, so
        # auto-set the billing mode rather than making the user pick it
        # manually. The price + DO_NOT_STORE guards below then apply. This
        # mirrors the model's ``clean()`` so the form and model agree even
        # when only one of them runs.
        if cleaned.get("x402_enabled"):
            cleaned["agent_billing_mode"] = AgentBillingMode.AGENT_PAYS_X402

        # ── History policy + contract-edit gate ───────────────────────
        # Versioned workflows that have runs, or are locked, have a
        # frozen validation contract: every past run executed under
        # specific file-type / retention rules. The gate blocks edits
        # that would invalidate that history (narrowing the file-type
        # set, shortening retention) while allowing edits that only
        # widen the contract (adding file types, extending retention).
        #
        # Mutable workflows intentionally trade away that reproducibility
        # guarantee. They can be edited in place after runs. History
        # policy itself cannot change in place once runs exist or the row
        # is locked: otherwise one workflow row would mix run guarantees
        # from before and after the policy switch.
        #
        # Direction matters because:
        # - Widening: past runs executed under broader-or-equal rules,
        #   so the new contract is a superset of what they relied on.
        #   Future runs accept more. No past run is invalidated.
        # - Narrowing: past runs relied on rules that no longer hold.
        #   A credential or audit lookup that says "this ran under
        #   workflow X" now means something different than before.
        #
        # Superusers bypass the gate entirely (for operational repairs)
        # and we record an audit entry so the integrity story remains
        # intact — the trail explains why the workflow definition
        # drifted in place.
        #
        # We run this gate AFTER the cascade above so the user gets one
        # clear error per affected field. ``self.instance`` still carries
        # the DB values here because ``_post_clean()`` hasn't merged
        # ``cleaned_data`` into it yet.
        self._superuser_overrode_contract_lock = False
        self._superuser_overridden_fields: set[str] = set()
        proposed_history_policy = (
            cleaned.get("history_policy")
            or getattr(self.instance, "history_policy", None)
            or WorkflowHistoryPolicy.VERSIONED
        )
        cleaned["history_policy"] = proposed_history_policy
        if self.instance and self.instance.pk and self.enforce_history_lock:
            current_history_policy = self.instance.history_policy
            if current_history_policy != proposed_history_policy and (
                self.instance.is_locked or self.instance.has_runs()
            ):
                self.requires_new_version_for_save = True
                self.add_error(
                    "history_policy",
                    ValidationError(
                        _(
                            "This workflow already has validation history or "
                            "is locked. Create a new workflow version to "
                            "change its history policy.",
                        ),
                        code="history_policy_locked",
                    ),
                )

        if (
            self.instance
            and self.instance.pk
            and self.enforce_history_lock
            and proposed_history_policy == WorkflowHistoryPolicy.VERSIONED
            and (self.instance.is_locked or self.instance.has_runs())
        ):
            is_superuser = self.user and getattr(
                self.user,
                "is_superuser",
                False,
            )
            if is_superuser:
                # Record which contract fields are being overridden so
                # the post-save audit entry can name them. Use the
                # narrower "unsafely changed" set rather than the raw
                # "changed" set — safe widenings don't need an audit
                # entry because they don't carry integrity risk.
                self._superuser_overridden_fields = (
                    self.instance.unsafely_changed_contract_fields(cleaned)
                )
                self._superuser_overrode_contract_lock = bool(
                    self._superuser_overridden_fields,
                )
            else:
                unsafe = self.instance.unsafely_changed_contract_fields(cleaned)
                if unsafe:
                    self.requires_new_version_for_save = True
                for field_name in unsafe:
                    self.add_error(
                        field_name,
                        ValidationError(
                            self._contract_lock_message(
                                field_name=field_name,
                                current=getattr(self.instance, field_name, None),
                                proposed=cleaned.get(field_name),
                            ),
                            code="contract_field_locked",
                        ),
                    )

        # ── Access controls must stay within the org's ceilings ─────────
        # The access fields only exist on the form when the user is allowed
        # to edit them (``_user_can_edit_access``). When present, each is
        # clamped to the organization's ceiling so an author can never set
        # a workflow wider than the org permits — a defence-in-depth check
        # alongside the clamped widget choices / disabled toggles (a
        # crafted POST could otherwise bypass the UI). The org-level
        # ceilings themselves are never editable here.
        if self._access_fields:
            from validibot.workflows.constants import WorkflowVisibility
            from validibot.workflows.constants import visibility_within_cap

            org = self._org_for_access()

            requested_visibility = cleaned.get("workflow_visibility")
            if not requested_visibility:
                # Omitted on a form that exposes the field: fall back to the
                # secure default rather than writing an empty tier. Keep the
                # current tier on edit; default to PRIVATE on create, mirroring
                # the model field so access can never silently widen.
                requested_visibility = (
                    self.instance.workflow_visibility
                    if self.instance and self.instance.pk
                    else WorkflowVisibility.PRIVATE
                )
                cleaned["workflow_visibility"] = requested_visibility

            # Same secure-default treatment for the billing rail: an omitted
            # mode must not write an empty value. Keep the current mode on
            # edit; default to AUTHOR_PAYS on create. (Enabling x402 re-selects
            # the x402 rail via the model cascade, so this never traps x402.)
            if not cleaned.get("agent_billing_mode"):
                from validibot.workflows.constants import AgentBillingMode

                cleaned["agent_billing_mode"] = (
                    self.instance.agent_billing_mode
                    if self.instance and self.instance.pk
                    else AgentBillingMode.AUTHOR_PAYS
                )

            if requested_visibility:
                cap = getattr(org, "workflow_visibility_cap", None) or (
                    WorkflowVisibility.ALL_USERS
                )
                if not visibility_within_cap(requested_visibility, cap):
                    self.add_error(
                        "workflow_visibility",
                        ValidationError(
                            _(
                                "That visibility exceeds the maximum allowed "
                                "by your organization.",
                            ),
                            code="visibility_exceeds_org_cap",
                        ),
                    )

            # MCP / x402 may only be enabled when the org permits the
            # channel. ``disabled`` widgets already coerce these to their
            # initial value, but we re-check here so a forged POST that
            # flips them on is rejected rather than silently saved.
            if cleaned.get("mcp_enabled") and not getattr(org, "mcp_allowed", False):
                self.add_error(
                    "mcp_enabled",
                    ValidationError(
                        _("MCP access is disabled for this organization."),
                        code="mcp_disabled_for_org",
                    ),
                )

            if cleaned.get("x402_enabled") and not getattr(org, "x402_allowed", False):
                self.add_error(
                    "x402_enabled",
                    ValidationError(
                        _("x402 access is disabled for this organization."),
                        code="x402_disabled_for_org",
                    ),
                )

        # ── x402 billing must pair with DO_NOT_STORE retention ─────────
        # x402 is anonymous per-call micropayment access.  Storing agent
        # submissions would undermine the privacy model that x402 enables.
        # Also enforced on the model so API/admin writes can't bypass it,
        # but surfacing the error on the form field gives better UX.
        #
        # agent_billing_mode only exists on the form for users allowed to
        # edit access, so .get() returns None otherwise and the check falls
        # through harmlessly.
        if (
            cleaned.get("agent_billing_mode") == AgentBillingMode.AGENT_PAYS_X402
            and cleaned.get("input_retention") != SubmissionRetention.DO_NOT_STORE
        ):
            self.add_error(
                "input_retention",
                ValidationError(
                    _(
                        "Input retention must be 'Do not store' when agents "
                        "pay via x402 micropayments — x402 is anonymous "
                        "per-call access and storing submissions is "
                        "incompatible with its privacy model."
                    ),
                    code="x402_requires_do_not_store",
                ),
            )

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

        # A workflow must always belong to a project: runs started from it
        # default to that project, and several downstream surfaces (analytics,
        # quotas, project-scoped views) assume a non-null project. Requiring it
        # here gives a friendly inline error instead of letting a project-less
        # workflow slip through; ``Workflow.clean()`` enforces the same rule at
        # the model layer for non-form paths.
        project_field.required = True
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

        # Write the access-control fields that are not in Meta.fields.
        # These are only present (in ``self._access_fields``) when the user
        # was allowed to edit access, so non-privileged users never touch
        # the workflow's audience. ``make_info_page_public`` is added to
        # ``update_fields`` because the model's ``save()`` auto-publishes
        # the info page for ALL_USERS visibility, and a narrow
        # ``update_fields`` would otherwise drop that synced flag.
        access_fields = getattr(self, "_access_fields", [])
        if self._user_can_edit_access() and access_fields:
            for field_name in access_fields:
                if field_name in self.cleaned_data:
                    setattr(workflow, field_name, self.cleaned_data[field_name])
            if commit and workflow.pk:
                workflow.save(
                    update_fields=[*access_fields, "make_info_page_public"],
                )

        if commit and workflow.pk:
            description_md = (self.cleaned_data.get("description_md") or "").strip()
            public_info = workflow.get_public_info
            if public_info.content_md != description_md:
                public_info.content_md = description_md
                public_info.save()

        # Record a superuser contract-lock override in the audit log so
        # the integrity story stays intact even though the workflow
        # definition drifted in place. We piggy-back on the existing
        # ``workflow_updated`` action with a structured ``metadata``
        # payload — that keeps the audit-action enum compact while
        # still giving compliance reviewers a searchable marker
        # (``metadata.contract_override = True``).
        if commit and workflow.pk and self._superuser_overrode_contract_lock:
            self._record_contract_override_audit(workflow)

        return workflow

    def _record_contract_override_audit(self, workflow: Workflow) -> None:
        """Write an audit entry naming the bypassed contract fields.

        Wraps the audit call in a broad ``except`` so a logging
        misconfiguration cannot prevent the actual save from
        succeeding. The audit subsystem is best-effort observability,
        not part of the save's correctness contract.
        """
        try:
            from validibot.audit.constants import AuditAction
            from validibot.audit.services import ActorSpec
            from validibot.audit.services import AuditLogService

            AuditLogService.record(
                action=AuditAction.WORKFLOW_UPDATED,
                actor=ActorSpec(user=self.user),
                org=workflow.org,
                target=workflow,
                metadata={
                    "contract_override": True,
                    "fields_overridden": sorted(
                        self._superuser_overridden_fields,
                    ),
                    "reason": ("superuser_in_place_edit_of_locked_workflow_contract"),
                },
            )
        except Exception:
            logger.exception(
                "Failed to write contract-override audit entry for workflow %s",
                workflow.pk,
            )


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
        # Mirror the model column so an over-long value is rejected with a form
        # error rather than reaching ``objects.create`` (which skips
        # ``full_clean``) and erroring at the DB as a 500. Single source:
        # VALIDATION_RUN_SHORT_DESCRIPTION_MAX_LENGTH.
        max_length=VALIDATION_RUN_SHORT_DESCRIPTION_MAX_LENGTH,
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
            "When enabled, submitters can view the schema on the workflow's "
            "public info page.",
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
      come from the validator's StepIODefinition rows.

    - **System FMU validator (step-level upload)**: The author uploads
      an FMU directly in the step form. The system introspects the
      FMU and stores discovered variables as ``StepIODefinition`` rows
      and simulation defaults in ``step.config["fmu_simulation"]``.
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
            self.fields["schema_text"].initial = (step.display_settings or {}).get(
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
            self.fields["schema_text"].initial = (step.display_settings or {}).get(
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


class ShaclStepConfigForm(ShaclConfigMixin, BaseStepConfigForm):
    """Collects SHACL step configuration: shapes, ontologies, engine knobs.

    The SHACL field declarations come from
    :class:`validibot.validations.validators.shacl.form_fields.ShaclConfigMixin`
    so the library-validator create/update forms can declare the same
    UI without duplication. The mixin contributes multi-file uploads
    for shapes and ontologies, an inline-text fallback each, bundled-
    standards checkboxes (Brick, QUDT — content ships in Phase 2),
    and the engine knobs (inference mode, advanced SHACL toggle,
    submission format override).

    The form's ``clean()`` runs an rdflib parse pass on every uploaded
    file so syntax errors surface immediately at save time rather than
    at validation time.

    See ADR-2026-05-18 for the architecture and the step config dialog
    spec this form implements.

    The cleaned data is consumed by ``build_shacl_config()`` in
    :mod:`validibot.workflows.views_helpers`, which writes the
    concatenated shapes Turtle to ``Ruleset.rules_text`` and the rest
    to ``Ruleset.metadata``.
    """

    show_display_schema = True
    SHACL_RESULT_HANDLING_CHOICES = (
        (
            SHACL_RESULT_FAIL_IMMEDIATELY,
            _("Fail immediately on violations"),
        ),
        (
            SHACL_RESULT_FAIL_AFTER_ASSERTIONS,
            _("Fail after assertions"),
        ),
        (
            SHACL_RESULT_REPORT_ONLY,
            _("Report only"),
        ),
    )
    shacl_result_handling = forms.ChoiceField(
        label=_("SHACL result handling"),
        choices=SHACL_RESULT_HANDLING_CHOICES,
        initial=SHACL_RESULT_HANDLING_DEFAULT,
        required=False,
        help_text=_(
            "Controls how SHACL validation results affect this step. "
            "Parse errors, invalid shapes, timeouts, and engine failures "
            "always fail the step immediately.",
        ),
    )

    def __init__(self, *args, step=None, **kwargs):
        super().__init__(*args, step=step, **kwargs)
        self.existing_shape_files: list[dict[str, Any]] = []
        self.existing_ontology_files: list[dict[str, Any]] = []
        self.has_existing_inline_shapes = False
        self.has_existing_inline_ontology = False
        self.library_default_snapshot: dict[str, Any] | None = None
        self.has_library_ontology = False
        self.fields["shacl_result_handling"].label = self._shacl_result_handling_label()
        if step and step.ruleset_id:
            self._initial_from_step(step)
        self.helper.layout = self._build_layout()

    def _initial_from_step(self, step) -> None:
        """Pre-fill engine knobs and inline text from a saved step.

        Multi-file uploads can't be pre-filled (browsers refuse to
        populate file inputs from server state), so on edit the existing
        uploaded files are shown read-only above the file fields.
        """
        config = step.config or {}
        # File-upload metadata, the shapes preview, and the library snapshot are
        # cosmetic (display duplicates of ruleset.metadata) and live in the
        # display bucket now; engine knobs below stay in ``config`` (semantic).
        # (ADR-2026-06-18.)
        display_settings = step.display_settings or {}
        metadata = dict(getattr(step.ruleset, "metadata", None) or {})
        self.existing_shape_files = (
            display_settings.get("shape_files") or metadata.get("shape_files") or []
        )
        self.existing_ontology_files = (
            display_settings.get("ontology_files")
            or metadata.get("ontology_files")
            or []
        )
        self.has_existing_inline_shapes = bool(
            metadata.get("has_inline_shapes")
            or (
                display_settings.get("shapes_text_preview")
                and not self.existing_shape_files
            )
        )
        self.has_existing_inline_ontology = bool(metadata.get("has_inline_ontology"))
        self.library_default_snapshot = display_settings.get(
            "library_default_snapshot"
        ) or metadata.get("library_default_snapshot")
        self.has_library_ontology = bool(
            metadata.get("library_default_inlined") and metadata.get("ontology_text")
        )
        if "inference_mode" in config:
            self.fields["inference_mode"].initial = config["inference_mode"]
        if "advanced_shacl" in config:
            self.fields["advanced_shacl"].initial = config["advanced_shacl"]
        if "submission_format" in config:
            self.fields["submission_format"].initial = config["submission_format"]
        if "shacl_result_handling" in config:
            self.fields["shacl_result_handling"].initial = config[
                "shacl_result_handling"
            ]
        bundled = set(config.get("bundled_standards", []) or [])
        if "bundle_brick" in self.fields:
            self.fields["bundle_brick"].initial = "brick-1.4" in bundled
        if "bundle_qudt" in self.fields:
            self.fields["bundle_qudt"].initial = "qudt-2.1" in bundled
        # On edit, surface the existing concatenated shapes for paste-area
        # preview but make it clear they're optional to replace.
        preview = display_settings.get("shapes_text_preview", "")
        if preview:
            self.fields["shapes_files"].help_text = _(
                "Leave blank to keep the current shapes. Upload one or more "
                "new SHACL Turtle shape files (.ttl) to replace them.",
            )
            self.fields["shapes_text"].help_text = _(
                "Leave blank to keep the existing shapes. Paste new "
                "shapes here (or upload files above) to replace them.",
            )
        if (
            self.existing_ontology_files
            or self.has_existing_inline_ontology
            or self.has_library_ontology
        ):
            self.fields["ontology_files"].help_text = _(
                "Leave blank to keep the current supplementary ontologies. "
                "Upload new Turtle ontology files (.ttl) to replace them.",
            )
            self.fields["ontology_text"].help_text = _(
                "Leave blank to keep the existing ontology text. Paste new "
                "ontology Turtle here (or upload files above) to replace it.",
            )

    def _build_layout(self) -> Layout:
        """Render SHACL configuration with saved-file summaries near uploads."""
        help_drawer_url = reverse(
            "core:help_drawer",
            kwargs={"slug": "shacl-validator"},
        )
        return Layout(
            Div(
                form_section_intro(
                    _("Basic settings"),
                    _(
                        "Name this step and control the basic information shown "
                        "to workflow authors and submitters."
                    ),
                ),
                "name",
                "description",
                "display_schema",
                "show_success_messages",
                "notes",
                css_class=APP_FORM_SECTION_CLASS,
            ),
            Div(
                form_section_intro(
                    _("SHACL shapes"),
                    _(
                        "Upload or paste the SHACL Turtle shapes that will be "
                        "merged and evaluated against the submitted RDF graph."
                    ),
                ),
                HTML(
                    self._existing_sources_html(
                        title=_("Current SHACL shapes"),
                        files=self.existing_shape_files,
                        has_inline=self.has_existing_inline_shapes,
                        inherited_snapshot=self.library_default_snapshot,
                        inline_label=_("Inline shapes are saved on this step."),
                        empty_keep_label=_(
                            "Leave the fields below blank to keep them."
                        ),
                    )
                ),
                "shapes_files",
                "shapes_text",
                css_class=APP_FORM_SECTION_CLASS,
            ),
            Div(
                form_section_intro(
                    _("Supplementary ontologies"),
                    _(
                        "Optionally provide ontology Turtle files that give the "
                        "reasoner extra class and property context."
                    ),
                ),
                HTML(
                    self._existing_sources_html(
                        title=_("Current supplementary ontologies"),
                        files=self.existing_ontology_files,
                        has_inline=self.has_existing_inline_ontology,
                        inherited_snapshot=(
                            self.library_default_snapshot
                            if self.has_library_ontology
                            else None
                        ),
                        inline_label=_("Inline ontology text is saved on this step."),
                        empty_keep_label=_(
                            "Leave the fields below blank to keep them."
                        ),
                    )
                ),
                "ontology_files",
                "ontology_text",
                css_class=APP_FORM_SECTION_CLASS,
            ),
            Div(
                self._advanced_options_intro(help_drawer_url),
                "shacl_result_handling",
                "inference_mode",
                "advanced_shacl",
                "submission_format",
                css_class=APP_FORM_SECTION_CLASS,
            ),
        )

    @staticmethod
    def _advanced_options_intro(help_drawer_url: str) -> HTML:
        """Render the SHACL advanced-options heading with drawer help."""

        return HTML(
            format_html(
                "<div class='d-flex align-items-start justify-content-between "
                "gap-3 mb-3'>"
                "<div>"
                "<h6 class='mb-1'>{}</h6>"
                "<p class='text-muted small mb-0'>{}</p>"
                "</div>"
                "<button type='button' class='btn btn-light btn-sm text-dark' "
                "data-help-drawer-trigger hx-get='{}' "
                "hx-target='#helpDrawerBody' hx-swap='innerHTML' "
                "data-bs-toggle='tooltip' data-bs-placement='top' "
                "title='{}' aria-label='{}'>"
                "<i class='bi bi-info-circle'></i>"
                "</button></div>",
                _("Advanced options"),
                _(
                    "Control SHACL result handling, inference, advanced SHACL "
                    "features, and serialization detection."
                ),
                help_drawer_url,
                _("SHACL validator help"),
                _("SHACL validator help"),
            ),
        )

    @staticmethod
    def _shacl_result_handling_label() -> str:
        """Return the result-handling label with a rich explanatory tooltip."""

        tooltip = render_to_string(
            "help/partials/shacl_result_handling_hint.html",
        ).strip()
        return format_html(
            "{} <span class='ms-1 align-middle help-tooltip'>"
            "<span tabindex='0' role='button' class='text-muted' "
            "data-bs-toggle='tooltip' data-bs-html='true' "
            "data-bs-custom-class='cel-tooltip shacl-tooltip' "
            "aria-label='{}'>"
            "<i class='bi bi-info-circle'></i>"
            "</span>"
            "<template class='cel-tooltip-content'>{}</template>"
            "</span>",
            _("SHACL result handling"),
            _("About SHACL result handling"),
            mark_safe(tooltip),  # noqa: S308
        )

    @staticmethod
    def _existing_sources_html(
        *,
        title: str,
        files: list[dict[str, Any]],
        has_inline: bool,
        inherited_snapshot: dict[str, Any] | None,
        inline_label: str,
        empty_keep_label: str,
    ) -> str:
        """Return a read-only summary of saved SHACL sources for edit forms."""
        if not files and not has_inline and not inherited_snapshot:
            return ""

        file_rows = ""
        if files:
            file_rows = format_html(
                "<ul class='mb-2 ps-3'>{}</ul>",
                format_html_join(
                    "",
                    (
                        "<li><span class='fw-semibold'>{}</span>"
                        "<span class='text-muted ms-2'>{}</span>"
                        "<span class='text-muted ms-2'>{}</span></li>"
                    ),
                    (
                        (
                            file_meta.get("name") or _("Unnamed file"),
                            filesizeformat(file_meta.get("size_bytes") or 0),
                            (
                                f"sha256:{str(file_meta.get('sha256', ''))[:12]}"
                                if file_meta.get("sha256")
                                else ""
                            ),
                        )
                        for file_meta in files
                    ),
                ),
            )

        inline_row = (
            format_html(
                "<div class='small mb-1'><i class='bi bi-file-text me-1'></i>{}</div>",
                inline_label,
            )
            if has_inline
            else ""
        )
        inherited_row = ""
        if inherited_snapshot:
            inherited_row = format_html(
                "<div class='small mb-1'><i class='bi bi-link-45deg me-1'></i>"
                "Inherited from library validator <span class='fw-semibold'>{}</span>"
                "</div>",
                inherited_snapshot.get("validator_slug") or _("unknown"),
            )

        return format_html(
            "<div class='alert alert-secondary py-2 px-3 mb-3'>"
            "<div class='fw-semibold mb-1'>{}</div>"
            "{}{}{}"
            "<div class='small text-muted'>{}</div>"
            "</div>",
            title,
            file_rows,
            inline_row,
            inherited_row,
            empty_keep_label,
        )

    def clean(self) -> dict[str, Any]:
        cleaned = super().clean()
        shape_files = cleaned.get("shapes_files") or []
        shape_text = (cleaned.get("shapes_text") or "").strip()
        ontology_files = cleaned.get("ontology_files") or []
        ontology_text = (cleaned.get("ontology_text") or "").strip()
        cleaned["shacl_result_handling"] = (
            cleaned.get("shacl_result_handling") or SHACL_RESULT_HANDLING_DEFAULT
        )
        keep_existing_shapes = bool(self.step and self.step.ruleset_id) and not (
            shape_files or shape_text
        )
        default_ruleset = getattr(
            getattr(self, "validator", None),
            "default_ruleset",
            None,
        )
        inherit_library_shapes = bool(
            default_ruleset and getattr(default_ruleset, "rules_text", "").strip(),
        ) and not (shape_files or shape_text)

        # At least one shapes source is required, unless we're editing an
        # existing step and the author left both blank (keep-existing
        # semantics, mirroring the JSON Schema form), or the selected
        # library SHACL validator already carries default shapes.
        if not (
            shape_files or shape_text or keep_existing_shapes or inherit_library_shapes
        ):
            err = _(
                "Provide at least one SHACL shape — upload one or more "
                "files or paste shapes inline.",
            )
            self.add_error("shapes_files", err)
            self.add_error("shapes_text", err)

        self.shacl_enforce_size_caps(shape_files, "shapes_files")
        self.shacl_enforce_size_caps(ontology_files, "ontology_files")

        # Surface Turtle/JSON-LD/RDF-XML parse errors as form errors
        # before the workflow saves. Cheaper-than-validation-time and
        # gives the author immediate feedback at edit time.
        self.shacl_syntax_pre_flight_files(shape_files, "shapes_files")
        self.shacl_syntax_pre_flight_files(ontology_files, "ontology_files")
        if shape_text:
            self.shacl_syntax_pre_flight_text(shape_text, "shapes_text")
        if ontology_text:
            self.shacl_syntax_pre_flight_text(ontology_text, "ontology_text")

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
                    # Cosmetic (display bucket) — ADR-2026-06-18.
                    "show_energyplus_warnings": (step.display_settings or {}).get(
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


class DisplayStepOutputsForm(forms.Form):
    """Form for selecting which output signals appear in submission results.

    Rendered inside a modal on the step detail page.  Populates choices
    from the validator's output signal definitions.  The selection is stored
    in ``step.config["display_step_outputs"]``.
    """

    display_step_outputs = forms.MultipleChoiceField(
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
            from validibot.validations.models import StepIODefinition

            step_outputs = StepIODefinition.objects.filter(
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
            from validibot.validations.models import StepIODefinition

            validator_outputs = StepIODefinition.objects.filter(
                validator=validator,
                direction="output",
            ).order_by("order", "pk")
            for sig in validator_outputs:
                key = sig.contract_key
                if key not in seen_keys:
                    seen_keys.add(key)
                    label = sig.label or sig.contract_key
                    choices.append((key, label))

        self.fields["display_step_outputs"].choices = choices

        # Pre-select currently displayed signals (cosmetic → display bucket,
        # ADR-2026-06-18).
        if step:
            current = (step.display_settings or {}).get("display_step_outputs", [])
            if current:
                self.fields["display_step_outputs"].initial = current


# ---------------------------------------------------------------------------
# Standalone form for editing template variable annotations from the step
# detail page's right-column card.  This form is instantiated by the
# StepEditorCardSpec plugin system — the EnergyPlus ValidatorConfig
# declares it as the form_class for its "template-variables" card.
# ---------------------------------------------------------------------------


def _build_template_vars_from_signals(step: Any) -> list[dict[str, Any]]:
    """Build template variable dicts from step-owned StepIODefinition rows.

    Reads ``StepIODefinition`` rows with ``origin_kind=TEMPLATE`` and their
    ``StepInputBinding`` to produce dicts that the template variable
    annotation form fields consume.
    """
    if not step or not step.pk:
        return []

    from validibot.validations.constants import SignalOriginKind
    from validibot.validations.models import StepInputBinding

    bindings = (
        StepInputBinding.objects.filter(
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
    variable metadata from step-owned ``StepIODefinition`` rows
    (``origin_kind=TEMPLATE``) and their ``StepInputBinding`` rows.

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

    Unlike ``TemplateVariableAnnotationForm`` which creates dynamic
    fields for all variables at once, this form handles one variable
    at a time. Used by the per-variable edit modal in the unified
    signals card.
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


# Delimiter choices for the tabular config form. Empty value = auto-detect
# (the reader sniffs the delimiter at read time).
TABULAR_DELIMITER_CHOICES = [
    ("", _("Auto-detect")),
    (",", _("Comma")),
    ("\t", _("Tab")),
    (";", _("Semicolon")),
    ("|", _("Pipe")),
]
# Sample uploads for inference are small by nature; cap to keep it cheap.
TABULAR_SAMPLE_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
TABULAR_SCHEMA_MAX_BYTES = 2 * 1024 * 1024  # 2 MB
TABULAR_COLUMN_FORMSET_PREFIX = "columns"
TABULAR_TYPE_CHOICES = [
    ("string", _("Text")),
    ("integer", _("Integer")),
    ("number", _("Number")),
    ("boolean", _("Boolean")),
    ("date", _("Date")),
    ("datetime", _("Date and time")),
]


class TabularColumnForm(forms.Form):
    """Edit one ordered field in a Frictionless Table Schema descriptor.

    ``original_name`` lets the serializer preserve imported field metadata
    that the editor does not expose. Authors can rename a field without losing
    keys such as ``title`` or ``description`` from the source descriptor.
    """

    original_name = forms.CharField(required=False, widget=forms.HiddenInput)
    name = forms.CharField(
        label=_("Column name"),
        max_length=255,
        widget=forms.TextInput(
            attrs={
                "class": "form-control",
                "placeholder": _("For example, decimalLatitude"),
                "autocomplete": "off",
            },
        ),
    )
    type = forms.ChoiceField(
        label=_("Data type"),
        choices=TABULAR_TYPE_CHOICES,
        initial="string",
        widget=forms.Select(attrs={"class": "form-select"}),
    )
    required = forms.BooleanField(
        label=_("Required"),
        required=False,
        help_text=_("The column must exist and its cells cannot be empty."),
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    unique = forms.BooleanField(
        label=_("Unique values"),
        required=False,
        help_text=_("Repeated non-empty values fail validation."),
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    primary_key = forms.BooleanField(
        label=_("Primary key"),
        required=False,
        help_text=_("Select multiple columns to define a composite key."),
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )
    required_when_present = forms.CharField(
        label=_("Required when another column exists"),
        required=False,
        help_text=_(
            "Require this column only when the selected companion column is "
            "present in the submitted file.",
        ),
        widget=forms.Select(
            choices=[("", _("Never (optional column)"))],
            attrs={"class": "form-select", "data-tabular-required-when": ""},
        ),
    )
    minimum = forms.FloatField(
        label=_("Minimum"),
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "any"}),
    )
    maximum = forms.FloatField(
        label=_("Maximum"),
        required=False,
        widget=forms.NumberInput(attrs={"class": "form-control", "step": "any"}),
    )
    min_length = forms.IntegerField(
        label=_("Minimum length"),
        required=False,
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0"}),
    )
    max_length = forms.IntegerField(
        label=_("Maximum length"),
        required=False,
        min_value=0,
        widget=forms.NumberInput(attrs={"class": "form-control", "min": "0"}),
    )
    pattern = forms.CharField(
        label=_("Pattern"),
        required=False,
        max_length=1000,
        help_text=_("A regular expression matched against non-empty values."),
        widget=forms.TextInput(
            attrs={
                "class": "form-control font-monospace",
                "placeholder": r"^[A-Z]{2}-\d+$",
                "spellcheck": "false",
            },
        ),
    )
    enum_values = forms.CharField(
        label=_("Allowed values"),
        required=False,
        help_text=_("Enter one allowed value per line."),
        widget=forms.Textarea(
            attrs={
                "class": "form-control font-monospace",
                "rows": 3,
                "placeholder": _("present\nabsent"),
                "spellcheck": "false",
            },
        ),
    )

    def clean(self):
        """Enforce constraints where the editor can give immediate feedback."""
        cleaned = super().clean()
        column_type = cleaned.get("type")
        minimum = cleaned.get("minimum")
        maximum = cleaned.get("maximum")
        min_length = cleaned.get("min_length")
        max_length = cleaned.get("max_length")
        pattern = (cleaned.get("pattern") or "").strip()
        required_when_present = (cleaned.get("required_when_present") or "").strip()

        if column_type not in {"integer", "number"} and (
            minimum is not None or maximum is not None
        ):
            self.add_error(
                "minimum",
                _("Numeric limits are only available for number columns."),
            )
        if minimum is not None and maximum is not None and minimum > maximum:
            self.add_error("maximum", _("Maximum must be greater than minimum."))

        if column_type != "string" and (
            min_length is not None or max_length is not None or pattern
        ):
            self.add_error(
                "min_length",
                _("Length and pattern rules are only available for text columns."),
            )
        if (
            min_length is not None
            and max_length is not None
            and min_length > max_length
        ):
            self.add_error(
                "max_length",
                _("Maximum length must be greater than minimum length."),
            )
        if pattern:
            # Validate with RE2 (the same engine native validation runs), so an
            # author learns at save time that a backreference/lookaround pattern
            # is unsupported — rather than it failing only when a file is checked.
            try:
                compile_user_pattern(pattern)
            except UnsafeOrInvalidPatternError as exc:
                self.add_error("pattern", str(exc))

        if cleaned.get("required") and required_when_present:
            self.add_error(
                "required_when_present",
                _(
                    "A column that is always required does not need a conditional "
                    "requirement.",
                ),
            )

        enum_values = [
            value.strip()
            for value in (cleaned.get("enum_values") or "").splitlines()
            if value.strip()
        ]
        duplicate_values = sorted(
            {value for value in enum_values if enum_values.count(value) > 1},
        )
        if duplicate_values:
            self.add_error(
                "enum_values",
                _("Allowed values must be unique. Duplicates: %(values)s")
                % {"values": ", ".join(duplicate_values)},
            )
        cleaned["enum_values"] = enum_values
        cleaned["pattern"] = pattern
        cleaned["required_when_present"] = required_when_present
        return cleaned


class BaseTabularColumnFormSet(forms.BaseFormSet):
    """Validate the ordered column collection as one addressable schema."""

    ordering_widget = forms.HiddenInput

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        names = [
            str(form["name"].value()).strip()
            for form in self.forms
            if form["name"].value()
        ]
        choices = [("", _("Never (optional column)"))]
        choices.extend((name, name) for name in names)
        for form in self.forms:
            form.fields["required_when_present"].widget.choices = choices

    def clean(self):
        """Reject duplicate and case-colliding names before persistence."""
        super().clean()
        if any(self.errors):
            return

        seen: dict[str, str] = {}
        active_names: set[str] = set()
        for form in self.forms:
            if self.can_delete and self._should_delete_form(form):
                continue
            name = (form.cleaned_data.get("name") or "").strip()
            key = name.casefold()
            if key in seen:
                raise ValidationError(
                    _(
                        "Column names must be unique, including letter case: "
                        "%(first)s and %(second)s conflict."
                    )
                    % {"first": seen[key], "second": name},
                )
            seen[key] = name
            active_names.add(name)

        for form in self.forms:
            if self.can_delete and self._should_delete_form(form):
                continue
            name = (form.cleaned_data.get("name") or "").strip()
            trigger = (form.cleaned_data.get("required_when_present") or "").strip()
            if not trigger:
                continue
            if trigger == name:
                form.add_error(
                    "required_when_present",
                    _("A column cannot make itself conditionally required."),
                )
            elif trigger not in active_names:
                form.add_error(
                    "required_when_present",
                    _(
                        "Select a companion column that is declared in this schema.",
                    ),
                )


TabularColumnFormSet = forms.formset_factory(
    TabularColumnForm,
    formset=BaseTabularColumnFormSet,
    extra=0,
    can_order=True,
    can_delete=True,
    min_num=1,
    max_num=1024,
    validate_min=True,
    validate_max=True,
)


def tabular_column_initial(descriptor: dict | None) -> list[dict[str, Any]]:
    """Convert a descriptor into ordered formset initial values."""
    if not isinstance(descriptor, dict):
        return []
    primary_key = descriptor.get("primaryKey") or []
    if isinstance(primary_key, str):
        primary_key = [primary_key]
    primary_names = {str(name) for name in primary_key}
    supported_types = {value for value, _label in TABULAR_TYPE_CHOICES}
    initial: list[dict[str, Any]] = []
    for raw_field in descriptor.get("fields") or []:
        if not isinstance(raw_field, dict) or not isinstance(
            raw_field.get("name"),
            str,
        ):
            continue
        constraints = raw_field.get("constraints")
        if not isinstance(constraints, dict):
            constraints = {}
        name = raw_field["name"]
        declared_type = raw_field.get("type", "string")
        enum_values = constraints.get("enum")
        initial.append(
            {
                "original_name": name,
                "name": name,
                "type": (
                    declared_type if declared_type in supported_types else "string"
                ),
                "required": bool(constraints.get("required")),
                "unique": bool(constraints.get("unique")),
                "primary_key": name in primary_names,
                "required_when_present": constraints.get(
                    "x-validibot-requiredWhenPresent",
                    "",
                ),
                "minimum": constraints.get("minimum"),
                "maximum": constraints.get("maximum"),
                "min_length": constraints.get("minLength"),
                "max_length": constraints.get("maxLength"),
                "pattern": constraints.get("pattern", ""),
                "enum_values": (
                    "\n".join(str(value) for value in enum_values)
                    if isinstance(enum_values, (list, tuple))
                    else ""
                ),
            },
        )
    return initial


def build_tabular_descriptor(
    column_formset: BaseTabularColumnFormSet,
    *,
    base_descriptor: dict | None = None,
) -> dict:
    """Serialize cleaned column forms while preserving unexposed metadata.

    Known field/constraint keys are replaced by the editor values. Unknown
    top-level, field-level, and constraint-level keys survive, so importing a
    richer descriptor and changing one range does not discard useful metadata.
    """
    descriptor = dict(base_descriptor or {})
    existing_fields = {
        raw.get("name"): raw
        for raw in descriptor.get("fields", [])
        if isinstance(raw, dict) and isinstance(raw.get("name"), str)
    }
    fields: list[dict[str, Any]] = []
    primary_key: list[str] = []
    constraint_keys = {
        "required",
        "unique",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "pattern",
        "enum",
        "x-validibot-requiredWhenPresent",
    }

    ordered_forms = (
        column_formset.ordered_forms
        if column_formset.can_order
        else column_formset.forms
    )
    for form in ordered_forms:
        if column_formset.can_delete and column_formset._should_delete_form(form):
            continue
        cleaned = form.cleaned_data
        name = cleaned["name"].strip()
        original_name = cleaned.get("original_name") or name
        raw_field = dict(existing_fields.get(original_name, {}))
        raw_constraints = raw_field.get("constraints")
        constraints = dict(raw_constraints) if isinstance(raw_constraints, dict) else {}
        for key in constraint_keys:
            constraints.pop(key, None)

        is_primary_key = bool(cleaned.get("primary_key"))
        if cleaned.get("required") or is_primary_key:
            constraints["required"] = True
        if cleaned.get("unique") and not is_primary_key:
            constraints["unique"] = True
        required_when_present = cleaned.get("required_when_present")
        if required_when_present and not constraints.get("required"):
            constraints["x-validibot-requiredWhenPresent"] = required_when_present
        value_map = {
            "minimum": cleaned.get("minimum"),
            "maximum": cleaned.get("maximum"),
            "minLength": cleaned.get("min_length"),
            "maxLength": cleaned.get("max_length"),
            "pattern": cleaned.get("pattern"),
            "enum": cleaned.get("enum_values"),
        }
        for key, value in value_map.items():
            if value not in (None, "", []):
                constraints[key] = value

        raw_field["name"] = name
        raw_field["type"] = cleaned["type"]
        if constraints:
            raw_field["constraints"] = constraints
        else:
            raw_field.pop("constraints", None)
        fields.append(raw_field)
        if is_primary_key:
            primary_key.append(name)

    descriptor["fields"] = fields
    if primary_key:
        descriptor["primaryKey"] = primary_key
    else:
        descriptor.pop("primaryKey", None)
    return descriptor


class TabularStepConfigForm(BaseStepConfigForm):
    """Settings form for a Tabular Validator step.

    Configures the file dialect (delimiter / header) and the column
    schema. The schema can be provided two ways: paste a Frictionless Table
    Schema descriptor, or upload a sample CSV to *infer* one (the inferred
    descriptor is stored and shown for the author to tighten next time). The
    descriptor is written to ``ruleset.rules_text`` and the dialect to
    ``ruleset.metadata`` by ``build_tabular_config``.

    Heavy tabular imports (pandas via the inference path) are deferred to the
    methods that need them, so this widely-imported forms module stays light.
    """

    show_display_schema = True

    delimiter = forms.ChoiceField(
        label=_("Delimiter"),
        choices=TABULAR_DELIMITER_CHOICES,
        required=False,
        initial="",
        help_text=_(
            "Leave on auto-detect unless the file uses an unusual separator.",
        ),
    )
    # Encoding is intentionally NOT an editable field in V1. Submitted content
    # reaches the validator already decoded as UTF-8 (Submission.get_content),
    # so a per-step encoding setting could not be honored without silently
    # corrupting non-UTF-8 input. The dialect is pinned to UTF-8 end-to-end;
    # honoring other encodings needs a raw-bytes read path (a future slice).
    has_header = forms.BooleanField(
        label=_("File has a header row"),
        required=False,
        initial=True,
    )
    table_schema = forms.CharField(
        label=_("Table Schema (Frictionless descriptor)"),
        widget=forms.Textarea(
            attrs={
                "rows": 8,
                "spellcheck": "false",
                "class": "form-control font-monospace",
                "placeholder": '{\n  "fields": [\n    ...\n  ]\n}',
            },
        ),
        required=False,
        help_text=_(
            "Paste a Frictionless Table Schema descriptor to populate the "
            "column editor.",
        ),
    )
    sample_file = forms.FileField(
        label=_("Infer from a sample file"),
        required=False,
        help_text=_(
            "Upload a small sample CSV to infer column names and types.",
        ),
        widget=forms.ClearableFileInput(
            attrs={
                "class": "form-control",
                "accept": ".csv,.tsv,text/csv,text/tab-separated-values",
            },
        ),
    )
    schema_file = forms.FileField(
        label=_("Upload a Table Schema descriptor"),
        required=False,
        help_text=_("Upload a JSON descriptor instead of pasting it."),
        widget=forms.ClearableFileInput(
            attrs={
                "class": "form-control",
                "accept": ".json,application/json",
            },
        ),
    )
    schema_base = forms.CharField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, step=None, **kwargs):
        super().__init__(*args, step=step, **kwargs)
        self.base_descriptor = self._descriptor_from_step(step)
        if step and step.ruleset_id:
            metadata = getattr(step.ruleset, "metadata", None) or {}
            self.fields["delimiter"].initial = metadata.get("delimiter", "") or ""
            self.fields["has_header"].initial = bool(metadata.get("has_header", True))
            self.fields["table_schema"].initial = ""
        self.fields["schema_base"].initial = json.dumps(self.base_descriptor)

        has_column_forms = f"{TABULAR_COLUMN_FORMSET_PREFIX}-TOTAL_FORMS" in self.data
        if self.is_bound and has_column_forms:
            self.column_formset = TabularColumnFormSet(
                data=self.data,
                prefix=TABULAR_COLUMN_FORMSET_PREFIX,
            )
        else:
            initial = tabular_column_initial(self.base_descriptor) or [{}]
            self.column_formset = TabularColumnFormSet(
                initial=initial,
                prefix=TABULAR_COLUMN_FORMSET_PREFIX,
            )

    @staticmethod
    def _descriptor_from_step(step) -> dict:
        """Load the current descriptor for formset initial values."""
        if not step or not step.ruleset_id or not step.ruleset.rules_text:
            return {}
        try:
            descriptor = json.loads(step.ruleset.rules_text)
        except (TypeError, json.JSONDecodeError):
            return {}
        return descriptor if isinstance(descriptor, dict) else {}

    def clean(self):
        """Resolve the schema source, then block edits that orphan assertions.

        ``_clean_schema_source`` determines the new descriptor; if the schema is
        actually changing, ``_block_if_orphans_assertions`` re-checks the step's
        existing row/column assertions against it and refuses the save when a
        rename/delete/retype would turn a saved assertion into a run-time error.
        """
        cleaned = self._clean_schema_source()
        descriptor = cleaned.get("descriptor")
        if descriptor is not None:
            self._block_if_orphans_assertions(descriptor)
        return cleaned

    def _block_if_orphans_assertions(self, descriptor: dict) -> None:
        """Reject a schema change that would invalidate existing assertions.

        Row/column assertions validate their column references *when the
        assertion is saved*, but the schema editor changes the columns out from
        under them. We re-check the ruleset's assertions against the new schema
        so a removed/renamed column (or a ``sum`` on a now-non-numeric column) is
        caught here with an actionable message instead of failing silently at
        validation time.
        """
        if not (self.step and self.step.ruleset_id):
            return
        from validibot.validations.validators.tabular.schema import parse_table_schema

        try:
            schema = parse_table_schema(descriptor)
        except (ValueError, TypeError):
            return  # An unparseable schema is reported by the editor itself.
        declared = set(schema.field_names())
        field_types = {field.name: field.type for field in schema.fields}
        numeric = {"integer", "number"}
        problems: list[str] = []
        for assertion in self.step.ruleset.assertions.all():
            stage = (assertion.options or {}).get("tabular_stage")
            if stage not in {"row", "column"}:
                continue
            expression = (assertion.rhs or {}).get("expr") or assertion.cel_cache or ""
            label = (assertion.target_display or expression or "").strip()
            if stage == "row":
                unknown = sorted(referenced_row_columns(expression) - declared)
            else:
                unknown = sorted(referenced_column_aggregates(expression) - declared)
            if unknown:
                problems.append(
                    _(
                        "“%(label)s” references column(s) no longer in the schema: "
                        "%(cols)s"
                    )
                    % {"label": label, "cols": ", ".join(unknown)},
                )
                continue
            if stage == "column":
                problems.extend(
                    _("“%(label)s” uses sum on the now-non-numeric column “%(col)s”.")
                    % {"label": label, "col": column}
                    for column, metric in referenced_column_metrics(expression)
                    if metric == "sum" and field_types.get(column) not in numeric
                )
        if problems:
            self.add_error(
                None,
                _(
                    "These column changes would break existing assertions — update "
                    "or remove them first: %(problems)s",
                )
                % {"problems": "; ".join(problems)},
            )

    def _clean_schema_source(self):
        cleaned = super().clean()
        pasted = (cleaned.get("table_schema") or "").strip()
        sample = cleaned.get("sample_file")
        schema_file = cleaned.get("schema_file")
        has_pasted = bool(pasted)
        has_sample = bool(sample)
        has_schema_file = bool(schema_file)

        if sum((has_pasted, has_sample, has_schema_file)) > 1:
            error = _(
                "Paste a descriptor, upload a descriptor, or upload a sample "
                "file; choose one source.",
            )
            self.add_error("table_schema", error)
            self.add_error("sample_file", error)
            self.add_error("schema_file", error)
            return cleaned

        # Import/inference takes precedence when the form is submitted without
        # HTMx. This is the progressive-enhancement path: the same two source
        # controls still work even if JavaScript is unavailable.
        if has_sample:
            if sample.size > TABULAR_SAMPLE_MAX_BYTES:
                self.add_error(
                    "sample_file",
                    _("Sample files must be 5 MB or smaller."),
                )
                return cleaned
            descriptor = self._infer_descriptor(sample)
            if descriptor is not None:
                cleaned["descriptor"] = descriptor
                cleaned["descriptor_json"] = json.dumps(descriptor, indent=2)
                cleaned["schema_source"] = "infer"
            return cleaned
        if has_pasted:
            descriptor = self._validate_descriptor(pasted)
            if descriptor is not None:
                cleaned["descriptor"] = descriptor
                cleaned["descriptor_json"] = pasted
                cleaned["schema_source"] = "text"
                cleaned["schema_warnings"] = self._descriptor_warnings(descriptor)
            return cleaned
        if has_schema_file:
            descriptor_json = self._read_schema_file(schema_file)
            if descriptor_json is None:
                return cleaned
            descriptor = self._validate_descriptor(descriptor_json, field="schema_file")
            if descriptor is not None:
                cleaned["descriptor"] = descriptor
                cleaned["descriptor_json"] = descriptor_json
                cleaned["schema_source"] = "upload"
                cleaned["schema_warnings"] = self._descriptor_warnings(descriptor)
            return cleaned

        if f"{TABULAR_COLUMN_FORMSET_PREFIX}-TOTAL_FORMS" in self.data:
            if not self.column_formset.is_valid():
                self.add_error(None, _("Review the highlighted column settings."))
                return cleaned
            base_descriptor = self._validate_schema_base(
                cleaned.get("schema_base"),
            )
            descriptor = build_tabular_descriptor(
                self.column_formset,
                base_descriptor=base_descriptor,
            )
            try:
                from validibot.validations.validators.tabular.schema import (
                    parse_table_schema,
                )

                parse_table_schema(descriptor)
            except (ValueError, TypeError) as exc:
                self.add_error(
                    None,
                    _("Invalid column schema: %(err)s") % {"err": exc},
                )
                return cleaned
            cleaned["descriptor"] = descriptor
            cleaned["descriptor_json"] = json.dumps(descriptor, indent=2)
            cleaned["schema_source"] = "editor"
            cleaned["schema_warnings"] = self._descriptor_warnings(descriptor)
            return cleaned

        if self.step and self.step.ruleset_id:
            cleaned["schema_source"] = "keep"
        else:
            self.add_error(
                "table_schema",
                _("Paste a Table Schema descriptor or upload a sample to infer one."),
            )
        return cleaned

    def _validate_schema_base(self, raw: str | None) -> dict:
        """Treat the hidden base descriptor as untrusted form input."""
        if not raw:
            return self.base_descriptor
        try:
            descriptor = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            self.add_error(
                None,
                _("The column editor state is invalid. Reload the page and try again."),
            )
            return {}
        if not isinstance(descriptor, dict):
            self.add_error(
                None,
                _("The column editor state is invalid. Reload the page and try again."),
            )
            return {}
        return descriptor

    def _build_dialect(self):
        from validibot.validations.validators.tabular.preflight import TabularDialect

        return TabularDialect(
            delimiter=(self.cleaned_data.get("delimiter") or None),
            # Encoding is pinned to UTF-8 in V1 (see the field comment above).
            encoding="utf-8",
            has_header=bool(self.cleaned_data.get("has_header")),
        )

    def _validate_descriptor(
        self,
        text: str,
        *,
        field: str = "table_schema",
    ) -> dict | None:
        from validibot.validations.validators.tabular.schema import parse_table_schema

        try:
            descriptor = json.loads(text)
        except json.JSONDecodeError as exc:
            self.add_error(
                field,
                _("Descriptor is not valid JSON: %(err)s") % {"err": exc},
            )
            return None
        try:
            parse_table_schema(descriptor)
        except (ValueError, TypeError) as exc:
            self.add_error(
                field,
                _("Invalid Table Schema: %(err)s") % {"err": exc},
            )
            return None
        return descriptor

    def _read_schema_file(self, uploaded) -> str | None:
        """Read a bounded UTF-8 JSON descriptor upload."""
        if uploaded.size > TABULAR_SCHEMA_MAX_BYTES:
            self.add_error(
                "schema_file",
                _("Descriptor files must be 2 MB or smaller."),
            )
            return None
        try:
            uploaded.seek(0)
            content = uploaded.read()
            uploaded.seek(0)
            return content.decode("utf-8-sig")
        except (AttributeError, UnicodeDecodeError):
            self.add_error(
                "schema_file",
                _("Descriptor files must be UTF-8 JSON."),
            )
            return None

    @staticmethod
    def _descriptor_warnings(descriptor: dict) -> list[str]:
        """Return author-facing compatibility notices for an imported schema."""
        from validibot.validations.validators.tabular.schema import (
            table_schema_compatibility_notices,
        )

        return [
            notice.message for notice in table_schema_compatibility_notices(descriptor)
        ]

    def _infer_descriptor(self, sample) -> dict | None:
        from validibot.validations.validators.tabular.infer import infer_table_schema
        from validibot.validations.validators.tabular.preflight import TabularReadError

        sample.seek(0)
        content = sample.read()
        sample.seek(0)
        if not isinstance(content, bytes):
            content = str(content).encode("utf-8")
        try:
            inferred = infer_table_schema(content, dialect=self._build_dialect())
        except TabularReadError as exc:
            self.add_error(
                "sample_file",
                _("Could not read the sample: %(err)s") % {"err": exc},
            )
            return None
        return inferred.descriptor


class SchematronStepConfigForm(BaseStepConfigForm):
    """Step configuration for the Schematron validator (ADR-2026-07-01 D2).

    Mirrors the XML Schema / SHACL authoring flow: the author pastes or
    uploads their Schematron rules (a ``.sch`` document — e.g. a published
    standard's official rules file), and the step's Ruleset stores the
    source. Editing an existing step with both fields blank keeps the
    saved rules ("keep" mode, as XSD). The optional documentation-URL
    template turns every finding's native rule id into a deep link to the
    publisher's rule text (D10).

    Compiled Schematron is executable XSLT, so the rules only ever run
    inside the sandboxed validator container — Django just performs the
    cheap authoring checks here (well-formed XML, Schematron root, size).
    """

    schematron_text = forms.CharField(
        label=_("Schematron rules"),
        widget=forms.Textarea(attrs={"rows": 12, "spellcheck": "false"}),
        required=False,
    )
    schematron_file = forms.FileField(
        label=_("Upload Schematron (.sch)"),
        required=False,
    )
    rule_doc_url_template = forms.CharField(
        label=_("Rule documentation URL template"),
        required=False,
        help_text=_(
            "Optional. A URL template with {rule_id} for deep-linking "
            "findings to published rule docs, e.g. "
            "https://docs.peppol.eu/poacc/billing/3.0/rules/#{rule_id}",
        ),
    )

    def __init__(self, *args, step=None, **kwargs):
        super().__init__(*args, step=step, **kwargs)
        if step and step.ruleset_id and step.ruleset:
            metadata = step.ruleset.metadata or {}
            self.fields["rule_doc_url_template"].initial = metadata.get(
                "rule_doc_url_template",
                "",
            )

    def clean(self):
        cleaned = super().clean()
        text = (cleaned.get("schematron_text") or "").strip()
        upload = cleaned.get("schematron_file")

        if not text and not upload:
            if self.step and self.step.ruleset_id:
                cleaned["schematron_source"] = "keep"
                return cleaned
            message = _("Paste Schematron rules or upload a .sch file.")
            self.add_error("schematron_text", message)
            self.add_error("schematron_file", message)
            return cleaned

        cleaned["schematron_source"] = "text" if text else "upload"
        field_name = "schematron_text" if text else "schematron_file"
        payload: str | None = text or None
        if payload is None and upload:
            upload.seek(0)
            raw_bytes = upload.read()
            upload.seek(0)
            try:
                payload = raw_bytes.decode("utf-8")
            except UnicodeDecodeError:
                self.add_error(
                    field_name,
                    _("Uploaded Schematron must be UTF-8 encoded."),
                )
                return cleaned

        from validibot.validations.validators.schematron.security import (
            SchematronSecurityError,
        )
        from validibot.validations.validators.schematron.security import (
            validate_schematron_source,
        )

        try:
            validate_schematron_source(payload)
        except SchematronSecurityError as exc:
            self.add_error(field_name, str(exc))
            return cleaned

        cleaned["schematron_payload"] = payload
        return cleaned


def get_config_form_class(validation_type: str) -> type[forms.Form]:
    mapping: dict[str, type[forms.Form]] = {
        ValidationType.BASIC: BasicStepConfigForm,
        ValidationType.JSON_SCHEMA: JsonSchemaStepConfigForm,
        ValidationType.XML_SCHEMA: XmlSchemaStepConfigForm,
        ValidationType.SCHEMATRON: SchematronStepConfigForm,
        ValidationType.SHACL: ShaclStepConfigForm,
        ValidationType.TABULAR: TabularStepConfigForm,
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

    Supports editing both ``StepIODefinition`` metadata (label,
    description, unit) and ``StepInputBinding`` configuration
    (source_data_path, default_value, is_required). For library-owned
    signals, definition fields are rendered as read-only; for
    step-owned signals, all fields are editable.
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
            "A payload path (e.g. <code>p.path.to.some_key_name</code>) or a signal "
            "reference (e.g. <code>s.some_signal_name</code>) that provides the value "
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
            # Display the path with its namespace prefix so the user sees
            # which scope it belongs to:
            #   SIGNAL → "s.solar_irradiance"
            #   SUBMISSION_PAYLOAD → "p.building.floor_area"
            from validibot.validations.constants import BindingSourceScope

            display_path = binding.source_data_path
            if (
                binding.source_scope == BindingSourceScope.SIGNAL
                and display_path
                and not display_path.startswith(("s.", "signal."))
            ):
                display_path = f"s.{display_path}"
            elif (
                binding.source_scope == BindingSourceScope.SUBMISSION_PAYLOAD
                and display_path
                and not display_path.startswith(("p.", "payload."))
            ):
                display_path = f"p.{display_path}"
            self.fields["source_data_path"].initial = display_path
            if binding.default_value is not None:
                self.fields["default_value"].initial = str(binding.default_value)
            self.fields["is_required"].initial = binding.is_required

        # Library-owned signals: definition fields are read-only.
        if signal_definition and signal_definition.validator_id:
            for field_name in ("label", "description", "unit"):
                self.fields[field_name].disabled = True

        # Non-editable paths: disable source_data_path when the signal's
        # value source is controlled by the validator (is_path_editable=False).
        if signal_definition and not signal_definition.is_path_editable:
            self.fields["source_data_path"].disabled = True

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
            from validibot.validations.constants import BindingSourceScope

            update_fields = ["default_value", "is_required"]

            # Only update path/scope when the field is editable. When
            # is_path_editable=False the field is disabled and Django
            # returns the empty value, not the existing binding value.
            if not self.fields["source_data_path"].disabled:
                raw_path = self.cleaned_data.get("source_data_path") or ""
                # Detect namespace prefixes and set the correct binding scope.
                #
                # s. / signal. → SIGNAL scope (workflow-level signals)
                #   e.g. "s.solar_irradiance" → path="solar_irradiance"
                #
                # p. / payload. → SUBMISSION_PAYLOAD scope (raw submission data)
                #   e.g. "p.building.floor_area" → path="building.floor_area"
                #
                # submission. is deliberately NOT recognized here (ADR-2026-06-03b):
                # binding a validator INPUT from submission metadata already has a
                # dedicated, typed path — the BindingSourceScope.SUBMISSION_METADATA
                # scope — so adding a second ``submission.`` spelling would create
                # two ways to express the same intent. The new ``submission.``
                # namespace is the rule-author-facing READER (used in assertions),
                # not a binding source; keep the two layers distinct.
                #
                # No prefix → preserve existing scope (could be UPSTREAM_STEP,
                # etc.) unless it was SIGNAL, in which case reset to
                # SUBMISSION_PAYLOAD since the user removed the s. prefix.
                if raw_path.startswith(("s.", "signal.")):
                    binding.source_data_path = raw_path.split(".", 1)[1]
                    binding.source_scope = BindingSourceScope.SIGNAL
                elif raw_path.startswith(("p.", "payload.")):
                    binding.source_data_path = raw_path.split(".", 1)[1]
                    binding.source_scope = BindingSourceScope.SUBMISSION_PAYLOAD
                else:
                    binding.source_data_path = raw_path
                    if binding.source_scope == BindingSourceScope.SIGNAL:
                        binding.source_scope = BindingSourceScope.SUBMISSION_PAYLOAD
                update_fields.extend(["source_data_path", "source_scope"])

            default_str = self.cleaned_data.get("default_value", "").strip()
            binding.default_value = default_str if default_str else None
            binding.is_required = self.cleaned_data.get("is_required", True)
            binding.save(update_fields=update_fields)


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


class WorkflowConstantForm(forms.Form):
    """Form for creating and editing workflow Constants (the ``c.*`` namespace).

    A Constant is a fixed, author-defined value referenced in assertions as
    ``c.<name>`` (ADR-2026-06-18). The form deliberately has only four fields —
    Name, Type, Value, Description — and **no** Source path / On missing /
    Default, because a constant comes from the workflow definition and can never
    be "missing" (contrast :class:`WorkflowSignalMappingForm`).

    Type is explicit (no "Auto"): the chosen ``data_type`` drives coercion of
    ``value`` via ``coerce_constant_value`` at clean time, so the constant's
    contract is guaranteed before save — including storing a ``NUMBER`` as an
    exact decimal string. Structured ``LIST``/``OBJECT`` values are entered as
    JSON in the value textarea.
    """

    name = forms.CharField(
        max_length=100,
        label=_("Constant name"),
        help_text=_("Used in CEL expressions as c.name."),
    )
    data_type = forms.ChoiceField(
        label=_("Type"),
        help_text=_("The value's type. List/Object are entered as JSON."),
    )
    value = forms.CharField(
        label=_("Value"),
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text=_(
            'For Number type "0.40"; String takes the text literally; '
            'List/Object are JSON (e.g. ["EUR", "GBP"]).'
        ),
    )
    description = forms.CharField(
        max_length=500,
        required=False,
        label=_("Description"),
        help_text=_("Optional note (e.g. 'agreed €/kWh per the 2026 contract')."),
    )

    def __init__(
        self,
        *args,
        workflow: Workflow | None = None,
        exclude_constant_id: int | None = None,
        **kwargs,
    ):
        from validibot.workflows.constants import WorkflowConstantType

        self.workflow = workflow
        self.exclude_constant_id = exclude_constant_id
        super().__init__(*args, **kwargs)

        # Populate type choices from the single source of truth.
        self.fields["data_type"].choices = WorkflowConstantType.choices

        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column("name", css_class="col-12 col-lg-6"),
                Column("data_type", css_class="col-12 col-lg-6"),
            ),
            "value",
            "description",
        )

    def clean_name(self) -> str:
        """Validate constant name: CEL identifier, not reserved, unique.

        Uses the constant-scoped helpers (NOT the signal uniqueness check), so a
        constant may share a bare name with a signal — the ``c.``/``s.`` prefix
        disambiguates.
        """
        from validibot.workflows.services.constants import validate_constant_name
        from validibot.workflows.services.constants import validate_constant_name_unique

        name = self.cleaned_data["name"].strip()

        errors = validate_constant_name(name)
        if errors:
            raise ValidationError(errors)

        if self.workflow:
            unique_errors = validate_constant_name_unique(
                workflow_id=self.workflow.pk,
                name=name,
                exclude_constant_id=self.exclude_constant_id,
            )
            if unique_errors:
                raise ValidationError(unique_errors)

        return name

    def clean(self):
        """Coerce ``value`` against the chosen ``data_type`` at form level.

        Surfaces a type/bounds problem on the ``value`` field (rather than as a
        non-field error) so the author sees it next to the input. The coerced,
        canonical value is stashed on ``cleaned_data["_coerced_value"]`` for
        ``save_constant``.
        """
        from validibot.workflows.services.constants import ConstantValueError
        from validibot.workflows.services.constants import coerce_constant_value

        cleaned = super().clean()
        data_type = cleaned.get("data_type")
        raw_value = cleaned.get("value")
        if data_type and raw_value is not None:
            try:
                cleaned["_coerced_value"] = coerce_constant_value(
                    data_type,
                    raw_value,
                )
            except ConstantValueError as exc:
                self.add_error("value", str(exc))
        return cleaned

    def save_constant(self, workflow: Workflow, *, instance=None):
        """Create or update a ``WorkflowConstant`` from cleaned data.

        When creating, auto-assigns the next position so the new constant
        appears at the end of the list (mirrors signal mappings).
        """
        from validibot.workflows.models import WorkflowConstant

        coerced_value = self.cleaned_data["_coerced_value"]

        if instance:
            instance.name = self.cleaned_data["name"]
            instance.data_type = self.cleaned_data["data_type"]
            instance.value = coerced_value
            instance.description = self.cleaned_data["description"]
            instance.save()
            return instance

        last_position = (
            WorkflowConstant.objects.filter(workflow=workflow)
            .order_by("-position")
            .values_list("position", flat=True)
            .first()
        )
        next_position = (last_position or 0) + 10

        return WorkflowConstant.objects.create(
            workflow=workflow,
            name=self.cleaned_data["name"],
            data_type=self.cleaned_data["data_type"],
            value=coerced_value,
            description=self.cleaned_data["description"],
            position=next_position,
        )
