from django import forms
from django.utils.html import strip_tags
from django.utils.text import normalize_newlines
from django.utils.translation import gettext_lazy as _

from simplevalidations.core.models import SupportMessage


class SupportMessageForm(forms.ModelForm):
    class Meta:
        model = SupportMessage
        fields = ["subject", "message"]
        labels = {
            "subject": _("Subject"),
            "message": _("Message"),
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

    def _clean_text_value(self, value: str) -> str:
        cleaned = normalize_newlines(strip_tags(value or "").strip())
        if not cleaned:
            raise forms.ValidationError(_("Please add a little more detail."))
        return cleaned

    def clean_subject(self) -> str:
        return self._clean_text_value(self.cleaned_data.get("subject", ""))

    def clean_message(self) -> str:
        return self._clean_text_value(self.cleaned_data.get("message", ""))
