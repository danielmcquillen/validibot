from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from simplevalidations.validations.constants import AssertionType
from simplevalidations.validations.constants import CustomValidatorType
from simplevalidations.validations.constants import Severity


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
    notes = forms.CharField(
        label=_("Notes"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=_("Optional notes shown to other authors in your org."),
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
    notes = forms.CharField(
        label=_("Notes"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class RulesetAssertionForm(forms.Form):
    """Form for creating/updating catalog-backed assertions."""

    assertion_type = forms.ChoiceField(
        label=_("Assertion Type"),
        choices=AssertionType.choices,
    )
    target_slug = forms.ChoiceField(
        label=_("Target Signal"),
        choices=(),
    )
    threshold_value = forms.DecimalField(
        label=_("Threshold value"),
        required=True,
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
        widget=forms.Textarea(
            attrs={
                "rows": 2,
                "placeholder": _("Use template variables like {{value}}"),
            }
        ),
    )

    def __init__(self, *args, catalog_choices=None, **kwargs):
        super().__init__(*args, **kwargs)
        catalog_choices = catalog_choices or []
        self.fields["target_slug"].choices = catalog_choices

    def populate_from_instance(self, assertion):
        self.initial.update(
            {
                "assertion_type": assertion.assertion_type,
                "target_slug": assertion.target_slug,
                "threshold_value": assertion.definition.get("value"),
                "severity": assertion.severity,
                "when_expression": assertion.when_expression,
                "message_template": assertion.message_template,
            }
        )

    def build_definition(self):
        return {
            "value": float(self.cleaned_data["threshold_value"]),
        }
