from __future__ import annotations

from django import forms
from django.utils.translation import gettext_lazy as _

from simplevalidations.marketing.constants import BLOCKLISTED_EMAIL_DOMAINS


class BetaWaitlistForm(forms.Form):
    ORIGIN_HERO = "hero"
    ORIGIN_FOOTER = "footer"
    ALLOWED_ORIGINS = {ORIGIN_HERO, ORIGIN_FOOTER}

    email = forms.EmailField(
        widget=forms.EmailInput(
            attrs={
                "class": "form-control",
                "autocomplete": "email",
                "placeholder": _("Your work email"),
                "required": True,
            },
        ),
        max_length=254,
    )
    origin = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
        initial=ORIGIN_HERO,
    )
    company = forms.CharField(
        label=_("Company"),
        required=False,
        widget=forms.HiddenInput(
            attrs={
                "autocomplete": "off",
            },
        ),
    )

    def clean_email(self) -> str:
        email = (self.cleaned_data.get("email") or "").strip().lower()
        try:
            local_part, domain = email.rsplit("@", 1)
        except ValueError as exc:  # pragma: no cover - handled by EmailField
            raise forms.ValidationError(
                _("Please provide a valid work email.")
            ) from exc

        if len(local_part) < 2:
            raise forms.ValidationError(_("Please provide a valid work email."))

        if domain in BLOCKLISTED_EMAIL_DOMAINS:
            raise forms.ValidationError(
                _(
                    "Please use your professional email address rather than a disposable inbox.",
                ),
            )
        return email

    def clean_origin(self) -> str:
        value = (self.data.get("origin") or self.ORIGIN_HERO).strip().lower()
        if value not in self.ALLOWED_ORIGINS:
            return self.ORIGIN_HERO
        return value

    def clean_company(self) -> str:
        value = self.cleaned_data.get("company", "")
        if value:
            self.add_error(
                None,
                _(
                    "Please leave the hidden field blank so we know you're human.",
                ),
            )
            raise forms.ValidationError(_("Please leave this field blank."))
        return value
