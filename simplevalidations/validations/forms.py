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
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from simplevalidations.submissions.constants import SubmissionDataFormat
from simplevalidations.validations.constants import AssertionOperator
from simplevalidations.validations.constants import AssertionType
from simplevalidations.validations.constants import CustomValidatorType
from simplevalidations.validations.constants import Severity
from simplevalidations.validations.constants import ValidationType


class CustomValidatorCreateForm(forms.Form):
    """Form used to capture metadata for a new custom validator."""

    name = forms.CharField(
        label=_("Name"),
        max_length=120,
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
        help_text=_("Permit authors to reference assertion targets not in the catalog."),
    )
    supported_data_formats = forms.ChoiceField(
        label=_("Supported data format"),
        choices=[
            (SubmissionDataFormat.JSON, SubmissionDataFormat.JSON.label),
            (SubmissionDataFormat.YAML, SubmissionDataFormat.YAML.label),
        ],
        required=True,
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
            "description",
            "version",
            Row(
                Column("allow_custom_assertion_targets", css_class="col-12 col-xl-6"),
                Column("supported_data_formats", css_class="col-12 col-xl-6"),
            ),
            "notes",
        )


class CustomValidatorUpdateForm(forms.Form):
    """Edit form for an existing custom validator."""

    name = forms.CharField(
        label=_("Name"),
        max_length=120,
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
        help_text=_("Permit authors to reference assertion targets not in the catalog."),
    )
    supported_data_formats = forms.ChoiceField(
        label=_("Supported data format"),
        choices=[
            (SubmissionDataFormat.JSON, SubmissionDataFormat.JSON.label),
            (SubmissionDataFormat.YAML, SubmissionDataFormat.YAML.label),
        ],
        required=True,
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


class RulesetAssertionForm(forms.Form):
    """Form for creating/updating catalog-backed assertions."""

    assertion_type = forms.ChoiceField(
        label=_("Assertion Type"),
        choices=AssertionType.choices,
    )
    target_field = forms.CharField(
        label=_("Target Field"),
        widget=forms.TextInput(
            attrs={
                "autocomplete": "off",
            },
        ),
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
        label=_("Message"),
        required=False,
        help_text=_(
            "Supports {{value}} style placeholders plus filters round, upper, lower, default."
        ),
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _("Use template variables like {{value}}"),
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
        target_slug_datalist_id="assertion-target-slug-options",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.catalog_choices = list(catalog_choices or [])
        self.catalog_entries = list(catalog_entries or [])
        self.catalog_entry_map = {
            entry.slug: entry for entry in self.catalog_entries if getattr(entry, "slug", None)
        }
        self.catalog_slugs = set(self.catalog_entry_map.keys())
        self.validator = validator
        self.target_slug_datalist_id = target_slug_datalist_id

        target_field = self.fields["target_field"]
        target_field.help_text = _(
            "Use dot notation for nested objects and [index] for lists, e.g. "
            "`data.error[0].message`.",
        )
        target_attrs = target_field.widget.attrs
        target_attrs.update(
            {
                "list": self.target_slug_datalist_id,
                "placeholder": _("Search or enter a custom path"),
            },
        )
        operator_choices = [("", _("(Select one)"))]
        operator_choices.extend(self._basic_operator_choices())
        self.fields["operator"].choices = operator_choices
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column("assertion_type", css_class="col-12 col-lg-4"),
                Column("target_field", css_class="col-12 col-lg-8"),
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
                Column("datetime_value", css_class="col-12 col-lg-6"),
                Column("collection_operator", css_class="col-12 col-lg-3"),
                Column("collection_value", css_class="col-12 col-lg-3"),
            ),
            "cel_expression",
            Row(
                Column("severity", css_class="col-12 col-lg-4"),
                Column("when_expression", css_class="col-12 col-lg-8"),
            ),
            "message_template",
        )

    def clean(self):
        cleaned = super().clean()
        self._resolve_target_field()
        assertion_type = cleaned.get("assertion_type")
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
            cleaned["rhs_payload"] = {"expr": expression}
            cleaned["options_payload"] = {}
            cleaned["resolved_operator"] = AssertionOperator.CEL_EXPR
            cleaned["cel_cache"] = expression
        return cleaned

    def _basic_operator_choices(self):
        return [
            (choice.value, choice.label)
            for choice in AssertionOperator
            if choice != AssertionOperator.CEL_EXPR
        ]

    def _resolve_target_field(self):
        value = (self.cleaned_data.get("target_field") or "").strip()
        if not value:
            raise ValidationError({"target_field": _("Provide a target.")})

        if value in self.catalog_slugs:
            self.cleaned_data["target_catalog_entry"] = self.catalog_entry_map[value]
            self.cleaned_data["target_field_value"] = ""
            return

        if not self._validator_allows_custom_targets():
            raise ValidationError(
                {
                    "target_field": _(
                        "Select one of the available catalog targets.",
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
        normalized = [
            self._require_literal_for_list(value).value for value in values
        ]
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


class ValidatorRuleForm(forms.Form):
    """Form for creating/updating validator-level rules (currently CEL only)."""

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
        label=_("Rule type"),
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

    def clean_rule_type(self):
        value = self.cleaned_data.get("rule_type")
        if value != ValidatorRuleType.CEL_EXPRESSION:
            raise ValidationError(_("Unsupported rule type."))
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
            "entry_type",
            "run_stage",
            "slug",
            "label",
            "data_type",
            "description",
            "is_required",
            "order",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_tag = False
        self.helper.layout = Layout(
            Row(
                Column("entry_type", css_class="col-12 col-md-6"),
                Column("run_stage", css_class="col-12 col-md-6"),
            ),
            Row(
                Column("slug", css_class="col-12 col-md-6"),
                Column("label", css_class="col-12 col-md-6"),
            ),
            Row(
                Column("data_type", css_class="col-12 col-md-6"),
                Column("order", css_class="col-12 col-md-6"),
            ),
            "description",
            Row(
                Column("is_required", css_class="col-12 col-md-6"),
            ),
        )

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
        }
        if assertion.target_catalog_id:
            initial["target_field"] = assertion.target_catalog.slug
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
            AssertionOperator.CONTAINS,
            AssertionOperator.NOT_CONTAINS,
            AssertionOperator.STARTS_WITH,
            AssertionOperator.ENDS_WITH,
        }:
            initial["comparison_value"] = rhs.get("value")
        elif operator in {
            AssertionOperator.ANY,
            AssertionOperator.ALL,
            AssertionOperator.NONE,
        }:
            initial["collection_operator"] = rhs.get("operator")
            initial["collection_value"] = rhs.get("value")
        initial["case_insensitive"] = options.get("case_insensitive", False)
        initial["unicode_fold"] = options.get("unicode_fold", False)
        initial["coerce_types"] = options.get("coerce_types", False)
        initial["treat_missing_as_null"] = options.get("treat_missing_as_null", False)
