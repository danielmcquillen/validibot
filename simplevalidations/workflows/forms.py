from __future__ import annotations

from django import forms

from simplevalidations.workflows.models import Workflow


class WorkflowForm(forms.ModelForm):
    """Simple form for creating and updating workflows."""

    class Meta:
        model = Workflow
        fields = ["name", "version"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Name your workflow",
                    "autofocus": True,
                },
            ),
            "version": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "e.g. 1.0",
                },
            ),
        }
        help_texts = {
            "version": "Optional label to help you track iterations.",
        }

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean_name(self):
        name = self.cleaned_data.get("name", "").strip()
        if not name:
            raise forms.ValidationError("Name is required.")
        return name
