from crispy_forms.helper import FormHelper
from crispy_forms.layout import Field
from crispy_forms.layout import Layout
from django import forms
from django.utils.html import strip_tags
from django.utils.text import normalize_newlines
from django.utils.translation import gettext_lazy as _

from validibot.core.models import SupportMessage


class SupportMessageForm(forms.ModelForm):
    class Meta:
        model = SupportMessage
        fields = ["subject", "message"]
        labels = {
            "subject": _("Subject"),
            "message": _("Message"),
        }
        error_messages = {
            "subject": {"required": _("Please add a little more detail.")},
            "message": {"required": _("Please add a little more detail.")},
        }
        widgets = {
            "subject": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "autocomplete": "off",
                },
            ),
            "message": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 5,
                    "placeholder": _("How can we help?"),
                },
            ),
        }

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.helper = FormHelper(self)
        self.helper.form_tag = False  # handled in template
        self.helper.label_class = "form-label fw-semibold"
        self.helper.disable_csrf = True
        self.helper.layout = Layout(
            Field("subject", wrapper_class="mb-3"),
            Field("message", wrapper_class="mb-3"),
        )

    def _clean_text_value(self, value: str) -> str:
        cleaned = normalize_newlines(strip_tags(value or "").strip())
        if not cleaned:
            raise forms.ValidationError(_("Please add a little more detail."))
        return cleaned

    def clean_subject(self) -> str:
        return self._clean_text_value(self.cleaned_data.get("subject", ""))

    def clean_message(self) -> str:
        return self._clean_text_value(self.cleaned_data.get("message", ""))
