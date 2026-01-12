from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation
from typing import Any

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Column
from crispy_forms.layout import Layout
from crispy_forms.layout import Row
from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from django.template.loader import render_to_string
from django.utils.html import format_html
from django.utils.safestring import SafeString
from django.utils.safestring import mark_safe
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from validibot.projects.models import Project
from validibot.submissions.constants import SubmissionDataFormat
from validibot.validations.constants import AssertionOperator
from validibot.validations.constants import AssertionType
from validibot.validations.constants import CatalogEntryType
from validibot.validations.constants import CatalogRunStage
from validibot.validations.constants import CustomValidatorType
from validibot.validations.constants import Severity
from validibot.validations.constants import ValidatorRuleType
from validibot.validations.models import ValidatorCatalogEntry


class CelHelpLabelMixin:
    """Provide a helper to append the CEL help tooltip to field labels."""

    @staticmethod
    def _cel_help_markup() -> SafeString:
        # Template content is developer-controlled, so mark_safe is appropriate here.
        return mark_safe(render_to_string("shared/cel_help_tooltip.html").strip())  # noqa: S308

    def _append_cel_help_to_label(self, field_name: str = "cel_expression") -> None:
        field = self.fields.get(field_name)
        if not field:
            return
        # Use format_html to safely escape the label while allowing our HTML wrapper.
        # _cel_help_markup() returns SafeString so format_html won't escape it.
        field.label = format_html(
            "<div class='d-flex flex-row justify-content-between'>{}{}</div>",
            field.label,
            self._cel_help_markup(),
        )


class CustomValidatorCreateForm(forms.Form):
    """Form used to capture metadata for a new custom validator."""

    name = forms.CharField(
        label=_("Name"),
        max_length=120,
    )
    short_description = forms.CharField(
        label=_("Short description"),
        max_length=255,
        required=False,
        help_text=_("Shown in lists and cards."),
    )
    description = forms.CharField(
        label=_("Description"),
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
    )
    custom_type = forms.ChoiceField(
        label=_("Validator Type"),
        choices=CustomValidatorType.choices,
    )
    version = forms.CharField(
        label=_("Version"),
        max_length=40,
        required=False,
        help_text=_("Version label (e.g. '1.0', '2025-01')."),
    )
    allow_custom_assertion_targets = forms.BooleanField(
        label=_("Allow custom assertion targets"),
        required=False,
        help_text=_(
            "Permit authors to reference assertion targets not in the catalog."
        ),
    )
    supported_data_formats = forms.ChoiceField(
        label=_("Supported data format"),
        choices=[
            (SubmissionDataFormat.JSON, SubmissionDataFormat.JSON.label),
            (SubmissionDataFormat.YAML, SubmissionDataFormat.YAML.label),
        ],
        required=False,
        initial=SubmissionDataFormat.JSON,
        help_text=_("Pick the single data format this validator will parse."),
    )
    notes = forms.CharField(
        label=_("Notes"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Optional notes shown to other authors in your org."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column("name", css_class="col-12 col-xl-7"),
                Column("custom_type", css_class="col-12 col-xl-5"),
            ),
            "short_description",
            "description",
            "version",
            Row(
                Column("allow_custom_assertion_targets", css_class="col-12 col-xl-6"),
                Column("supported_data_formats", css_class="col-12 col-xl-6"),
            ),
            "notes",
        )


class FMIValidatorCreateForm(forms.Form):
    """Upload form used to create an FMI validator backed by an FMU asset."""

    name = forms.CharField(
        label=_("Name"),
        max_length=120,
    )
    short_description = forms.CharField(
        label=_("Short description"),
        max_length=255,
        required=False,
        help_text=_("Shown in lists and cards."),
    )
    description = forms.CharField(
        label=_("Description"),
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
    )
    project = forms.ModelChoiceField(
        label=_("Project"),
        queryset=Project.objects.none(),
        required=False,
        help_text=_("Optional project scope for this validator."),
    )
    fmu_file = forms.FileField(
        label=_("FMU file"),
        help_text=_("Upload an FMU archive containing modelDescription.xml."),
    )

    def __init__(self, *args, org=None, **kwargs):
        super().__init__(*args, **kwargs)
        qs = Project.objects.none()
        if org:
            qs = Project.objects.filter(org=org).order_by("name")
        self.fields["project"].queryset = qs
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column("name", css_class="col-12 col-xl-6"),
                Column("project", css_class="col-12 col-xl-6"),
            ),
            "short_description",
            "description",
            "fmu_file",
        )

    def clean_fmu_file(self):
        uploaded = self.cleaned_data.get("fmu_file")
        if not uploaded:
            raise ValidationError(_("Upload an FMU archive."))
        if uploaded.size <= 0:
            raise ValidationError(_("Uploaded file is empty."))
        if not uploaded.name.lower().endswith(".fmu"):
            raise ValidationError(_("Expected a .fmu file."))
        return uploaded


class CustomValidatorUpdateForm(forms.Form):
    """Edit form for an existing custom validator."""

    name = forms.CharField(
        label=_("Name"),
        max_length=120,
    )
    short_description = forms.CharField(
        label=_("Short description"),
        max_length=255,
        required=False,
        help_text=_("Shown in lists and cards."),
    )
    description = forms.CharField(
        label=_("Description"),
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
    )
    version = forms.CharField(
        label=_("Version"),
        max_length=40,
        required=False,
        help_text=_("Version label (e.g. '1.0', '2025-01')."),
    )
    allow_custom_assertion_targets = forms.BooleanField(
        label=_("Allow custom assertion targets"),
        required=False,
        help_text=_(
            "Permit authors to reference assertion targets not in the catalog."
        ),
    )
    supported_data_formats = forms.ChoiceField(
        label=_("Supported data format"),
        choices=[
            (SubmissionDataFormat.JSON, SubmissionDataFormat.JSON.label),
            (SubmissionDataFormat.YAML, SubmissionDataFormat.YAML.label),
        ],
        required=False,
        help_text=_("Pick the single data format this validator will parse."),
    )
    notes = forms.CharField(
        label=_("Notes"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(Column("name", css_class="col-12")),
            "short_description",
            "description",
            "version",
            Row(
                Column("allow_custom_assertion_targets", css_class="col-12 col-md-6"),
                Column("supported_data_formats", css_class="col-12 col-md-6"),
            ),
            "notes",
        )


CUSTOM_ASSERTION_TARGET_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_\-]*"
    r"(?:"
    r"(?:\.[A-Za-z_][A-Za-z0-9_\-]*)"
    r"|"
    r"(?:\[[0-9]+\])"
    r")*$",
)

TOLERANCE_MODE_CHOICES = (
    ("absolute", _("Absolute value")),
    ("percent", _("Percent of target")),
)

CEL_SIGNAL_FUNCTIONS = (
    "series",
    "signal",
    "value",
)

COLLECTION_OPERATOR_CHOICES = [
    (AssertionOperator.EQ, AssertionOperator.EQ.label),
    (AssertionOperator.NE, AssertionOperator.NE.label),
    (AssertionOperator.LT, AssertionOperator.LT.label),
    (AssertionOperator.LE, AssertionOperator.LE.label),
    (AssertionOperator.GT, AssertionOperator.GT.label),
    (AssertionOperator.GE, AssertionOperator.GE.label),
    (AssertionOperator.CONTAINS, AssertionOperator.CONTAINS.label),
    (AssertionOperator.STARTS_WITH, AssertionOperator.STARTS_WITH.label),
    (AssertionOperator.ENDS_WITH, AssertionOperator.ENDS_WITH.label),
    (AssertionOperator.MATCHES, AssertionOperator.MATCHES.label),
]


@dataclass(frozen=True)
class _LiteralValue:
    """Helper container for normalized literal values."""

    value: Any
    source: str


class RulesetAssertionForm(CelHelpLabelMixin, forms.Form):
    """Form for creating/updating catalog-backed assertions."""

    assertion_type = forms.ChoiceField(
        label=_("Assertion Type"),
        choices=AssertionType.choices,
    )
    target_field = forms.CharField(
        label=_("Target Signal"),
        required=False,
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
            },
        ),
    )
    target_catalog_entry = forms.ChoiceField(
        label=_("Catalog Signal"),
        required=False,
        choices=[],
    )
    operator = forms.ChoiceField(
        label=_("Condition"),
        choices=[],
        required=False,
    )
    comparison_value = forms.CharField(
        label=_("Value"),
        required=False,
    )
    comparison_value_secondary = forms.CharField(
        label=_("Second value"),
        required=False,
    )
    list_values = forms.CharField(
        label=_("Values (one per line)"),
        widget=forms.Textarea(attrs={"rows": 3}),
        required=False,
    )
    regex_pattern = forms.CharField(
        label=_("Regular expression"),
        required=False,
    )
    include_min = forms.BooleanField(
        label=_("Include minimum"),
        required=False,
        initial=True,
    )
    include_max = forms.BooleanField(
        label=_("Include maximum"),
        required=False,
        initial=True,
    )
    case_insensitive = forms.BooleanField(
        label=_("Case-insensitive"),
        required=False,
    )
    unicode_fold = forms.BooleanField(
        label=_("Unicode/locale fold"),
        required=False,
    )
    coerce_types = forms.BooleanField(
        label=_("Coerce numeric strings"),
        required=False,
    )
    treat_missing_as_null = forms.BooleanField(
        label=_("Treat missing as null"),
        required=False,
    )
    tolerance_value = forms.DecimalField(
        label=_("Tolerance"),
        required=False,
    )
    tolerance_mode = forms.ChoiceField(
        label=_("Tolerance type"),
        required=False,
        choices=TOLERANCE_MODE_CHOICES,
    )
    datetime_value = forms.DateTimeField(
        label=_("Date/Time"),
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    collection_operator = forms.ChoiceField(
        label=_("Collection member condition"),
        required=False,
        choices=COLLECTION_OPERATOR_CHOICES,
    )
    collection_value = forms.CharField(
        label=_("Collection member value"),
        required=False,
    )
    severity = forms.ChoiceField(
        label=_("Severity"),
        choices=Severity.choices,
        initial=Severity.ERROR,
    )
    when_expression = forms.CharField(
        label=_("When (optional)"),
        required=False,
        widget=forms.TextInput(
            attrs={
                "placeholder": _("CEL expression, e.g., has(series('fac_elec'))"),
            }
        ),
    )
    message_template = forms.CharField(
        label=_("Failure message"),
        required=False,
        help_text=_(
            "Shown when the assertion fails. "
            "Supports {{value}} style placeholders plus filters round, "
            "upper, lower, default."
        ),
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _("Use template variables like {{value}}"),
            }
        ),
    )
    success_message = forms.CharField(
        label=_("Success message"),
        required=False,
        help_text=_(
            "Shown when the assertion passes. "
            "Supports {{value}} style placeholders."
        ),
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _("e.g. EUI is within acceptable range"),
            }
        ),
    )
    cel_expression = forms.CharField(
        label=_("CEL expression"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )

    def __init__(
        self,
        *args,
        catalog_choices=None,
        catalog_entries=None,
        validator=None,
        target_slug_datalist_id=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.catalog_choices = list(catalog_choices or [])
        derived_enabled = getattr(settings, "ENABLE_DERIVED_SIGNALS", False)
        self.catalog_entries = [
            entry
            for entry in list(catalog_entries or [])
            if derived_enabled or entry.entry_type == CatalogEntryType.SIGNAL
        ]
        self.catalog_entry_map = {}
        self.inputs_by_slug: dict[str, ValidatorCatalogEntry] = {}
        self.outputs_by_slug: dict[str, ValidatorCatalogEntry] = {}
        self.choice_map: dict[str, ValidatorCatalogEntry] = {}
        for entry in self.catalog_entries:
            if entry.run_stage == CatalogRunStage.OUTPUT:
                self.outputs_by_slug.setdefault(entry.slug, entry)
            else:
                self.inputs_by_slug.setdefault(entry.slug, entry)
        self.catalog_slugs = set(
            list(self.inputs_by_slug.keys()) + list(self.outputs_by_slug.keys())
        )
        self.validator = validator
        self.target_slug_datalist_id = target_slug_datalist_id
        signal_choices = []
        for entry in self.catalog_entries:
            role = (
                _("Output") if entry.run_stage == CatalogRunStage.OUTPUT else _("Input")
            )
            label = entry.label or entry.slug
            value = f"{entry.run_stage}:{entry.slug}"
            self.choice_map[value] = entry
            signal_choices.append((value, f"{label} · {role}"))
        self.no_signal_choices = len(signal_choices) == 0
        self.fields["target_catalog_entry"].choices = [
            ("", _("Select a signal")),
            *signal_choices,
        ]
        # Hide catalog selector in favor of the target field; we
        # still keep it for backend resolution.
        self.fields["target_catalog_entry"].widget = forms.HiddenInput()
        if self.no_signal_choices:
            self.fields["target_catalog_entry"].required = False

        if self.fields.get("cel_expression"):
            if self._validator_allows_custom_targets():
                cel_help_text = _(
                    "You may enter new targets using dot notation "
                    "(e.g., data.error.message) "
                    "and [index] for lists."
                )
            else:
                cel_help_text = _(
                    "Only catalog targets are available for this validator."
                )
            self.fields["cel_expression"].help_text = cel_help_text

        target_field = self.fields["target_field"]
        if self._validator_allows_custom_targets():
            if self.no_signal_choices:
                target_field.label = _("Target Path")
                target_field.help_text = _(
                    "Use dot notation for nested objects and [index] for lists, e.g. "
                    "`payload.results[0].value`"
                )
            else:
                target_field.label = _("Target Signal or Path")
                target_field.help_text = _(
                    "Use `output.<name>` to "
                    "disambiguate output signals when an input signal shares "
                    "the same name. "
                    "Use dot notation for nested objects and [index] for lists, e.g. "
                    "`payload.results[0].value`.",
                )
        else:
            target_field.label = _("Target Signal")
            target_field.help_text = _(
                "Use `output.<name>` to "
                "disambiguate output signals when an input shares the same name.",
            )
        target_attrs = target_field.widget.attrs
        target_attrs.update(
            {
                "placeholder": _("Search or enter a custom path"),
            },
        )
        if self.target_slug_datalist_id:
            target_attrs.update(
                {
                    "list": self.target_slug_datalist_id,
                },
            )
        operator_choices = [("", _("(Select one)"))]
        operator_choices.extend(self._basic_operator_choices())
        self.fields["operator"].choices = operator_choices
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column("assertion_type", css_class="col-12 col-lg-3"),
                Column("cel_expression", css_class="col-12 col-lg-9"),
            ),
            Row(
                Column("target_field", css_class="col-12"),
            ),
            Row(
                Column("operator", css_class="col-12"),
            ),
            Row(
                Column("comparison_value", css_class="col-12 col-lg-6"),
                Column("comparison_value_secondary", css_class="col-12 col-lg-6"),
            ),
            "list_values",
            "regex_pattern",
            Row(
                Column("include_min", css_class="col-6 col-lg-3"),
                Column("include_max", css_class="col-6 col-lg-3"),
                Column("case_insensitive", css_class="col-6 col-lg-3"),
                Column("unicode_fold", css_class="col-6 col-lg-3"),
            ),
            Row(
                Column("coerce_types", css_class="col-6 col-lg-3"),
                Column("treat_missing_as_null", css_class="col-6 col-lg-3"),
                Column("tolerance_value", css_class="col-6 col-lg-3"),
                Column("tolerance_mode", css_class="col-6 col-lg-3"),
            ),
            Row(
                Column("datetime_value", css_class="col-12 col-lg-4"),
                Column("collection_operator", css_class="col-12 col-lg-4"),
                Column("collection_value", css_class="col-12 col-lg-4"),
            ),
            Row(
                Column("severity", css_class="col-12 col-lg-4"),
                Column("when_expression", css_class="col-12 col-lg-8"),
            ),
            "message_template",
        )
        self._append_cel_help_to_label("cel_expression")

    def clean(self):
        cleaned = super().clean()
        assertion_type = cleaned.get("assertion_type")
        if assertion_type == AssertionType.CEL_EXPRESSION:
            # CEL expressions declare their own targets inside the expression.
            self.cleaned_data["target_catalog_entry"] = None
            self.cleaned_data["target_field_value"] = ""
        else:
            self._resolve_target_field()
        if assertion_type == AssertionType.BASIC:
            operator_value = cleaned.get("operator")
            if not operator_value:
                raise ValidationError({"operator": _("Select a condition.")})
            rhs, options = self._build_basic_payload(AssertionOperator(operator_value))
            cleaned["rhs_payload"] = rhs
            cleaned["options_payload"] = options
            cleaned["resolved_operator"] = operator_value
            cleaned["cel_expression"] = ""
            cleaned["cel_cache"] = self._build_cel_preview(
                operator_value,
                rhs,
                options,
                cleaned.get("when_expression") or "",
            )
        else:
            expression = self._clean_cel_expression()
            if not self._validator_allows_custom_targets():
                self._validate_cel_identifiers(expression)
            cleaned["rhs_payload"] = {"expr": expression}
            cleaned["options_payload"] = {}
            cleaned["resolved_operator"] = AssertionOperator.CEL_EXPR
            cleaned["cel_cache"] = expression
            # Ensure the target constraint is satisfied for CEL assertions.
            cleaned["target_catalog_entry"] = None
            cleaned["target_field_value"] = expression or "__cel__"
        return cleaned

    def _basic_operator_choices(self):
        return [
            (choice.value, choice.label)
            for choice in AssertionOperator
            if choice != AssertionOperator.CEL_EXPR
        ]

    def _validate_cel_identifiers(self, expression: str) -> None:
        reserved_literals = {"true", "false", "null", "payload", "output"}
        cel_builtins = {
            "has",
            "exists",
            "exists_one",
            "all",
            "map",
            "filter",
            "size",
            "contains",
            "startsWith",
            "endsWith",
            "type",
            "int",
            "double",
            "string",
            "bool",
            "abs",
            "ceil",
            "floor",
            "round",
            "timestamp",
            "duration",
            "matches",
            "in",
        }
        allowed = set(self.catalog_slugs)
        allowed.update(f"output.{slug}" for slug in self.outputs_by_slug)
        unknown = set()
        for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_\\.]*", expression):
            name = match.group(0)
            if name in reserved_literals or name in cel_builtins:
                continue
            if len(name) == 1:
                continue
            if name not in allowed:
                unknown.add(name)
        if unknown:
            raise ValidationError(
                {
                    "cel_expression": _("Unknown signal(s) referenced: %(names)s")
                    % {"names": ", ".join(sorted(unknown))}
                }
            )

    def _resolve_target_field(self):
        catalog_choice = (self.cleaned_data.get("target_catalog_entry") or "").strip()
        value = (self.cleaned_data.get("target_field") or "").strip()

        if catalog_choice:
            entry = self.choice_map.get(catalog_choice)
            if not entry:
                raise ValidationError(
                    {
                        "target_field": _(
                            "Unknown signal(s) referenced. Provide a catalog signal "
                            "or enable custom targets."
                        )
                    }
                )
            self.cleaned_data["target_catalog_entry"] = entry
            self.cleaned_data["target_field_value"] = ""
            return

        if value:
            # Explicit output prefix wins.
            explicit_output = False
            if value.startswith("output."):
                explicit_output = True
                value = value.replace("output.", "", 1)

            if explicit_output and value in self.outputs_by_slug:
                self.cleaned_data["target_catalog_entry"] = self.outputs_by_slug[value]
                self.cleaned_data["target_field_value"] = ""
                return

            if value in self.inputs_by_slug:
                if value in self.outputs_by_slug and not explicit_output:
                    raise ValidationError(
                        {
                            "target_field": _(
                                "Both an input and output are named '%(name)s'. "
                                "Use `output.%(name)s` to target the output signal."
                            )
                            % {"name": value}
                        }
                    )
                self.cleaned_data["target_catalog_entry"] = self.inputs_by_slug[value]
                self.cleaned_data["target_field_value"] = ""
                return

            if value in self.outputs_by_slug:
                if value in self.inputs_by_slug and not explicit_output:
                    raise ValidationError(
                        {
                            "target_field": _(
                                "Both an input and output are named '%(name)s'. "
                                "Use `output.%(name)s` to target the output signal."
                            )
                            % {"name": value}
                        }
                    )
                self.cleaned_data["target_catalog_entry"] = self.outputs_by_slug[value]
                self.cleaned_data["target_field_value"] = ""
                return

            if not self._validator_allows_custom_targets():
                raise ValidationError(
                    {
                        "target_field": _(
                            "Unknown signal(s) referenced. Provide a catalog signal "
                            "or enable custom targets."
                        ),
                    },
                )
            if not CUSTOM_ASSERTION_TARGET_PATTERN.match(value):
                raise ValidationError(
                    {
                        "target_field": _(
                            "Custom targets must use dot notation with optional "
                            "numeric indexes, e.g. `data.results[0].value`.",
                        ),
                    },
                )
            self.cleaned_data["target_catalog_entry"] = None
            self.cleaned_data["target_field_value"] = value
            return

        raise ValidationError(
            {
                "target_field": _(
                    "Unknown signal(s) referenced. Provide a catalog "
                    "signal or enable custom targets."
                )
            }
        )

    def _validator_allows_custom_targets(self) -> bool:
        return bool(getattr(self.validator, "allow_custom_assertion_targets", False))

    def _build_basic_payload(
        self,
        operator: AssertionOperator,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        options = self._collect_common_options()
        if operator in {
            AssertionOperator.EQ,
            AssertionOperator.NE,
            AssertionOperator.LT,
            AssertionOperator.LE,
            AssertionOperator.GT,
            AssertionOperator.GE,
            AssertionOperator.LEN_EQ,
            AssertionOperator.LEN_LE,
            AssertionOperator.LEN_GE,
            AssertionOperator.TYPE_IS,
        }:
            literal = self._require_literal("comparison_value")
            rhs = {"value": literal.value}
            if operator == AssertionOperator.TYPE_IS:
                rhs["value"] = str(literal.source)
            return rhs, options

        if operator in {
            AssertionOperator.BETWEEN,
            AssertionOperator.COUNT_BETWEEN,
        }:
            lower = self._require_literal("comparison_value")
            upper = self._require_literal("comparison_value_secondary")
            rhs = {"min": lower.value, "max": upper.value}
            options["include_min"] = bool(self.cleaned_data.get("include_min"))
            options["include_max"] = bool(self.cleaned_data.get("include_max"))
            return rhs, options

        if operator in {
            AssertionOperator.IN,
            AssertionOperator.NOT_IN,
            AssertionOperator.SUBSET,
            AssertionOperator.SUPERSET,
        }:
            rhs = {"values": self._extract_list_literals()}
            return rhs, options

        if operator == AssertionOperator.UNIQUE:
            return {}, options

        if operator in {
            AssertionOperator.CONTAINS,
            AssertionOperator.NOT_CONTAINS,
            AssertionOperator.STARTS_WITH,
            AssertionOperator.ENDS_WITH,
        }:
            literal = self._require_literal("comparison_value", prefer_number=False)
            return {"value": literal.value}, options

        if operator == AssertionOperator.MATCHES:
            pattern = (self.cleaned_data.get("regex_pattern") or "").strip()
            if not pattern:
                raise ValidationError(
                    {"regex_pattern": _("Provide a regular expression.")},
                )
            return {"pattern": pattern}, options

        if operator in {AssertionOperator.IS_NULL, AssertionOperator.NOT_NULL}:
            return {}, options

        if operator in {AssertionOperator.IS_EMPTY, AssertionOperator.NOT_EMPTY}:
            return {}, options

        if operator == AssertionOperator.APPROX_EQ:
            base = self._require_literal("comparison_value")
            tolerance = self.cleaned_data.get("tolerance_value")
            if tolerance is None:
                raise ValidationError(
                    {"tolerance_value": _("Provide a tolerance value.")},
                )
            mode = self.cleaned_data.get("tolerance_mode")
            if not mode:
                raise ValidationError(
                    {"tolerance_mode": _("Select a tolerance type.")},
                )
            rhs = {"value": base.value, "tolerance": float(tolerance)}
            options["tolerance_mode"] = mode
            return rhs, options

        if operator in {AssertionOperator.BEFORE, AssertionOperator.AFTER}:
            dt_value = self.cleaned_data.get("datetime_value")
            if not dt_value:
                raise ValidationError(
                    {"datetime_value": _("Provide a date/time value.")},
                )
            return {"value": dt_value.isoformat()}, options

        if operator == AssertionOperator.WITHIN:
            literal = self._require_literal("comparison_value", prefer_number=False)
            return {"value": literal.value}, options

        if operator in {
            AssertionOperator.ANY,
            AssertionOperator.ALL,
            AssertionOperator.NONE,
        }:
            nested_operator = self.cleaned_data.get("collection_operator")
            if not nested_operator:
                raise ValidationError(
                    {
                        "collection_operator": _(
                            "Select a condition for the collection members.",
                        ),
                    },
                )
            nested_literal = self._require_literal("collection_value")
            rhs = {
                "operator": nested_operator,
                "value": nested_literal.value,
            }
            return rhs, options

        raise ValidationError(
            {"operator": _("Operator %(op)s is not supported.") % {"op": operator}}
        )

    def _collect_common_options(self) -> dict[str, Any]:
        option_fields = (
            "case_insensitive",
            "unicode_fold",
            "coerce_types",
            "treat_missing_as_null",
        )
        options: dict[str, Any] = {}
        for field in option_fields:
            if self.cleaned_data.get(field):
                options[field] = True
        return options

    def _require_literal(
        self,
        field_name: str,
        *,
        prefer_number: bool = True,
    ) -> _LiteralValue:
        raw = self.cleaned_data.get(field_name)
        if raw is None:
            raw = ""
        raw_text = str(raw).strip()
        if not raw_text:
            raise ValidationError(
                {field_name: _("Provide a value.")},
            )
        if prefer_number:
            try:
                return _LiteralValue(float(Decimal(raw_text)), raw_text)
            except InvalidOperation:
                pass
        lowered = raw_text.lower()
        if lowered in {"true", "false"}:
            return _LiteralValue(lowered == "true", raw_text)
        if lowered == "null":
            return _LiteralValue(None, raw_text)
        return _LiteralValue(raw_text, raw_text)

    def _extract_list_literals(self) -> list[Any]:
        raw = self.cleaned_data.get("list_values") or ""
        values = [line.strip() for line in raw.splitlines() if line.strip()]
        if not values:
            raise ValidationError(
                {"list_values": _("Provide at least one value (one per line).")},
            )
        normalized = [self._require_literal_for_list(value).value for value in values]
        return normalized

    def _require_literal_for_list(self, value: str) -> _LiteralValue:
        try:
            return _LiteralValue(float(Decimal(value)), value)
        except InvalidOperation:
            lowered = value.lower()
            if lowered in {"true", "false"}:
                return _LiteralValue(lowered == "true", value)
            if lowered == "null":
                return _LiteralValue(None, value)
            return _LiteralValue(value, value)

    def _clean_cel_expression(self) -> str:
        expression = (self.cleaned_data.get("cel_expression") or "").strip()
        if not expression:
            raise ValidationError(
                {"cel_expression": _("Provide a CEL expression.")},
            )
        if not self._delimiters_balanced(expression):
            raise ValidationError(
                {
                    "cel_expression": _(
                        "Parentheses and brackets must be balanced.",
                    ),
                },
            )
        if not self._validator_allows_custom_targets():
            unknown = self._find_unknown_cel_slugs(expression)
            if unknown:
                raise ValidationError(
                    {
                        "cel_expression": _(
                            "Unknown signal(s) referenced: %(names)s",
                        )
                        % {"names": ", ".join(sorted(unknown))},
                    },
                )
        return expression

    def _delimiters_balanced(self, expression: str) -> bool:
        pairs = {"(": ")", "[": "]", "{": "}"}
        stack: list[str] = []
        for char in expression:
            if char in pairs:
                stack.append(pairs[char])
            elif char in pairs.values():
                if not stack or stack.pop() != char:
                    return False
        return not stack

    def _find_unknown_cel_slugs(self, expression: str) -> set[str]:
        allowed = set(self.catalog_slugs) | {"payload"}
        target = (self.cleaned_data.get("target_field") or "").strip()
        if target:
            allowed.add(target)
        if self._validator_allows_custom_targets():
            return set()
        identifiers = {
            match.group(0)
            for match in re.finditer(r"[A-Za-z_][A-Za-z0-9_\.]*", expression)
        }
        return {ident for ident in identifiers if ident not in allowed}

    def _build_cel_preview(
        self,
        operator: str,
        rhs: dict[str, Any],
        options: dict[str, Any],
        when_expression: str,
    ) -> str:
        left = self._target_identifier()
        op = AssertionOperator(operator)
        expr = ""
        literal = rhs.get("value")
        formatter = self._format_literal
        if op == AssertionOperator.LE:
            expr = f"{left} <= {formatter(literal)}"
        elif op == AssertionOperator.LT:
            expr = f"{left} < {formatter(literal)}"
        elif op == AssertionOperator.GE:
            expr = f"{left} >= {formatter(literal)}"
        elif op == AssertionOperator.GT:
            expr = f"{left} > {formatter(literal)}"
        elif op == AssertionOperator.EQ:
            expr = f"{left} == {formatter(literal)}"
        elif op == AssertionOperator.NE:
            expr = f"{left} != {formatter(literal)}"
        elif op == AssertionOperator.BETWEEN:
            min_cmp = ">=" if options.get("include_min", True) else ">"
            max_cmp = "<=" if options.get("include_max", True) else "<"
            expr = (
                f"({left} {min_cmp} {formatter(rhs.get('min'))} && "
                f"{left} {max_cmp} {formatter(rhs.get('max'))})"
            )
        elif op == AssertionOperator.MATCHES:
            expr = f"re.matches({formatter(rhs.get('pattern'))}, {left})"
        elif op == AssertionOperator.IN:
            values = ", ".join(formatter(val) for val in rhs.get("values", []))
            expr = f"{left} in [{values}]"
        elif op == AssertionOperator.NOT_IN:
            values = ", ".join(formatter(val) for val in rhs.get("values", []))
            expr = f"!({left} in [{values}])"
        elif op == AssertionOperator.CEL_EXPR:
            expr = rhs.get("expr", "")
        else:
            expr = ""
        if when_expression:
            expr = f"({when_expression}) && ({expr})" if expr else when_expression
        return expr.strip()

    def _target_identifier(self) -> str:
        if self.cleaned_data.get("target_catalog_entry"):
            return self.cleaned_data["target_catalog_entry"].slug
        return self.cleaned_data.get("target_field_value") or ""

    def _format_literal(self, value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        escaped = str(value).replace('"', r"\"")
        return f'"{escaped}"'

    @classmethod
    def initial_from_instance(cls, assertion):
        initial = {
            "assertion_type": assertion.assertion_type,
            "severity": assertion.severity,
            "when_expression": assertion.when_expression,
            "message_template": assertion.message_template,
            "success_message": assertion.success_message,
        }
        if assertion.target_catalog_entry_id:
            initial["target_catalog_entry"] = assertion.target_catalog_entry.slug
            initial["target_field"] = ""
        else:
            initial["target_field"] = assertion.target_field
        if assertion.assertion_type == AssertionType.BASIC:
            initial["operator"] = assertion.operator
            rhs = assertion.rhs or {}
            options = assertion.options or {}
            cls._apply_operator_initial(initial, assertion.operator, rhs, options)
        else:
            initial["cel_expression"] = (assertion.rhs or {}).get("expr", "")
        return initial

    @staticmethod
    def _apply_operator_initial(initial, operator, rhs, options):
        if operator in {
            AssertionOperator.EQ,
            AssertionOperator.NE,
            AssertionOperator.LT,
            AssertionOperator.LE,
            AssertionOperator.GT,
            AssertionOperator.GE,
            AssertionOperator.LEN_EQ,
            AssertionOperator.LEN_LE,
            AssertionOperator.LEN_GE,
            AssertionOperator.TYPE_IS,
        }:
            initial["comparison_value"] = rhs.get("value")
        elif operator in {
            AssertionOperator.BETWEEN,
            AssertionOperator.COUNT_BETWEEN,
        }:
            initial["comparison_value"] = rhs.get("min")
            initial["comparison_value_secondary"] = rhs.get("max")
            initial["include_min"] = options.get("include_min", True)
            initial["include_max"] = options.get("include_max", True)
        elif operator in {
            AssertionOperator.IN,
            AssertionOperator.NOT_IN,
            AssertionOperator.SUBSET,
            AssertionOperator.SUPERSET,
        }:
            values = rhs.get("values") or []
            initial["list_values"] = "\n".join(str(value) for value in values)
        elif operator == AssertionOperator.MATCHES:
            initial["regex_pattern"] = rhs.get("pattern")
        elif operator == AssertionOperator.APPROX_EQ:
            initial["comparison_value"] = rhs.get("value")
            initial["tolerance_value"] = rhs.get("tolerance")
            initial["tolerance_mode"] = options.get("tolerance_mode")
        elif operator in {AssertionOperator.BEFORE, AssertionOperator.AFTER}:
            initial["datetime_value"] = rhs.get("value")
        elif operator in {
            AssertionOperator.ANY,
            AssertionOperator.ALL,
            AssertionOperator.NONE,
        }:
            initial["collection_operator"] = options.get("collection_operator")
            initial["collection_value"] = rhs.get("value")
        elif operator == AssertionOperator.CEL_EXPR:
            initial["cel_expression"] = rhs.get("expr", "")
        return initial


class ValidatorRuleForm(CelHelpLabelMixin, forms.Form):
    """Form for creating/updating validator-level default assertions (CEL only)."""

    name = forms.CharField(
        label=_("Name"),
        max_length=200,
    )
    description = forms.CharField(
        label=_("Description"),
        widget=forms.Textarea(attrs={"rows": 2}),
        required=False,
    )
    rule_type = forms.ChoiceField(
        label=_("Assertion type"),
        choices=ValidatorRuleType.choices,
    )
    cel_expression = forms.CharField(
        label=_("CEL expression"),
        widget=forms.Textarea(attrs={"rows": 4}),
        help_text=_("Enter a CEL expression that references validator signals."),
    )
    order = forms.IntegerField(
        label=_("Order"),
        min_value=0,
        required=False,
        initial=0,
        help_text=_("Lower numbers run first."),
    )
    signals = forms.MultipleChoiceField(
        label=_("Signals referenced"),
        required=False,
    )

    def __init__(self, *args, **kwargs):
        signal_choices = kwargs.pop("signal_choices", [])
        super().__init__(*args, **kwargs)
        self.fields["signals"].choices = signal_choices
        # Signals are auto-detected from the CEL expression; render as read-only.
        self.fields["signals"].disabled = True
        self.fields["signals"].help_text = _(
            "Signals are detected from the CEL expression and shown for reference."
        )
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column("name", css_class="col-12 col-xl-8"),
                Column("order", css_class="col-12 col-xl-4"),
            ),
            "description",
            "rule_type",
            "signals",
            "cel_expression",
        )
        self._append_cel_help_to_label("cel_expression")
        cel_label = self.fields["cel_expression"].label
        # Use format_html to safely escape the label
        self.fields["cel_expression"].label = format_html(
            '<span class="w-100 d-block">{}</span>',
            cel_label,
        )

    def clean_rule_type(self):
        value = self.cleaned_data.get("rule_type")
        if value != ValidatorRuleType.CEL_EXPRESSION:
            raise ValidationError(_("Unsupported assertion type."))
        return value

    def clean_cel_expression(self):
        expr = (self.cleaned_data.get("cel_expression") or "").strip()
        if not expr:
            raise ValidationError(_("CEL expression is required."))
        return expr


class ValidatorCatalogEntryForm(forms.ModelForm):
    """Form for creating/updating validator catalog entries (signals/derivations)."""

    class Meta:
        model = ValidatorCatalogEntry
        fields = [
            "run_stage",
            "slug",
            "target_field",
            "input_binding_path",
            "label",
            "data_type",
            "description",
            "is_required",
            "is_hidden",
            "default_value",
            "order",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        self.validator = kwargs.pop("validator", None)
        super().__init__(*args, **kwargs)
        self.validator = self.validator or getattr(self.instance, "validator", None)
        self.fields["label"].required = False
        self.fields["label"].widget = forms.HiddenInput()
        self.fields["label"].initial = ""
        self.fields["slug"].label = _("Signal name")
        self.fields["slug"].help_text = _(
            "Short, slug-form name (lowercase letters, numbers, hyphens) used in "
            "assertions and CEL expressions."
        )
        self.fields["slug"].validators = []
        self.fields["slug"].error_messages["required"] = _("Signal name is required.")
        self.fields["description"].help_text = _(
            "A short description to help you remember what data this signal represents."
        )
        self.fields["is_required"].help_text = _(
            "Requires the signal to be present in the submission (inputs) or "
            "processor output (outputs)."
        )
        if not getattr(settings, "ENABLE_DERIVED_SIGNALS", False):
            # Always treat as signal and hide type selection.
            self.instance.entry_type = CatalogEntryType.SIGNAL
            self.fields["entry_type"] = forms.CharField(
                initial=CatalogEntryType.SIGNAL,
                widget=forms.HiddenInput(),
            )
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column("run_stage", css_class="col-12 col-md-6"),
            ),
            Row(
                Column("slug", css_class="col-12 col-md-6"),
                Column("target_field", css_class="col-12 col-md-6"),
            ),
            Row(
                Column("data_type", css_class="col-12 col-md-6"),
            ),
            "description",
            Row(
                Column("order", css_class="col-12 col-md-6"),
                Column("is_required", css_class="col-12 col-md-6"),
            ),
            Row(
                Column("is_hidden", css_class="col-12 col-md-6"),
                Column("default_value", css_class="col-12 col-md-6"),
            ),
        )

    def clean_slug(self):
        value = (self.cleaned_data.get("slug") or "").strip()
        if not value:
            raise ValidationError(_("Signal name is required."))
        suggested = slugify(value)
        if suggested != value:
            raise ValidationError(
                _(
                    "Use slug format (lowercase letters, "
                    "numbers, hyphens). Try: %(suggested)s"
                )
                % {"suggested": suggested or _("a-slug-name")}
            )
        if not self.validator:
            return value
        existing = ValidatorCatalogEntry.objects.filter(
            validator=self.validator,
            slug=value,
        ).exclude(pk=self.instance.pk)
        if existing.exists():
            raise ValidationError(
                _(
                    "Signal name must be unique across inputs and "
                    "outputs for this validator."
                )
            )
        return value

    def clean(self):
        cleaned = super().clean()
        entry_type = cleaned.get("entry_type")
        if entry_type == CatalogEntryType.DERIVATION:
            cleaned["is_required"] = False
            self.cleaned_data["is_required"] = False
        if not cleaned.get("is_hidden"):
            cleaned["default_value"] = None
            self.cleaned_data["default_value"] = None
        cleaned["label"] = ""
        self.cleaned_data["label"] = ""
        return cleaned
