from __future__ import annotations

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Field
from crispy_forms.layout import Layout
from django import forms
from django.utils.translation import gettext_lazy as _

from simplevalidations.workflows.models import Workflow


class WorkflowForm(forms.ModelForm):
    """Simple form for creating and updating workflows."""

    class Meta:
        model = Workflow
        fields = [
            "name",
            "slug",
            "version",
        ]
        help_texts = {
            "version": "Optional label to help you track iterations.",
        }

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

        self.helper = FormHelper()
        self.helper.form_tag = False  # Disable automatic form tag generation
        self.helper.layout = Layout(
            Field(
                "name",
                placeholder="Name your workflow",
                autofocus=True,
                css_class="form-control",
            ),
            Field(
                "slug",
                placeholder="",
                css_class="form-control",
            ),
            Field(
                "version",
                placeholder="e.g. 1.0",
                css_class="form-control",
                default="1.0",
            ),
        )

    def clean_name(self):
        name = self.cleaned_data.get("name", "").strip()
        if not name:
            err_msg = _("Name is required.")
            raise forms.ValidationError(err_msg)
        return name
