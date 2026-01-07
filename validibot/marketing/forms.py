from __future__ import annotations

from crispy_forms.helper import FormHelper
from crispy_forms.layout import Div
from crispy_forms.layout import Field
from crispy_forms.layout import Layout
from crispy_forms.layout import Submit
from django import forms
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from validibot.marketing.constants import BLOCKLISTED_EMAIL_DOMAINS

EXPECTED_EMAIL_PARTS_LENGTH = 2


class BetaWaitlistForm(forms.Form):
    ORIGIN_HERO = "hero"
    ORIGIN_FOOTER = "footer"
    ALLOWED_ORIGINS = {ORIGIN_HERO, ORIGIN_FOOTER}
    FORM_TARGETS = {
        ORIGIN_HERO: "beta-waitlist-card",
        ORIGIN_FOOTER: "footer-waitlist",
    }
    FORM_IDS = {
        ORIGIN_HERO: "beta-waitlist-form",
        ORIGIN_FOOTER: "footer-waitlist-form",
    }

    email = forms.EmailField(
        label="",
        widget=forms.EmailInput(
            attrs={
                "class": "form-control",
                "autocomplete": "email",
                "placeholder": _("Your email"),
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
                _("Please provide a valid work email."),
            ) from exc

        if len(local_part) < EXPECTED_EMAIL_PARTS_LENGTH:
            raise forms.ValidationError(_("Please provide a valid work email."))

        if domain in BLOCKLISTED_EMAIL_DOMAINS:
            raise forms.ValidationError(
                _(
                    "Please use your professional email address "
                    "rather than a disposable inbox.",
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

    def __init__(
        self,
        *args,
        origin: str | None = None,
        target_id: str | None = None,
        **kwargs,
    ):
        initial = dict(kwargs.get("initial", {}) or {})
        data = args[0] if args else None

        origin_value = (
            (origin or "")
            or (data.get("origin") if hasattr(data, "get") else None)
            or initial.get("origin")
            or self.ORIGIN_HERO
        )
        origin_value = (origin_value or "").strip().lower()
        if origin_value not in self.ALLOWED_ORIGINS:
            origin_value = self.ORIGIN_HERO

        initial["origin"] = origin_value
        kwargs["initial"] = initial

        super().__init__(*args, **kwargs)

        self.origin_value = origin_value
        resolved_target = target_id or self.FORM_TARGETS.get(
            origin_value,
            "beta-waitlist-card",
        )
        form_id = self.FORM_IDS.get(origin_value, "beta-waitlist-form")

        if origin_value == self.ORIGIN_FOOTER:
            email_class = "form-control form-control-sm"
            submit_class = "btn btn-secondary btn-sm"
            email_wrapper = "mb-2 "
            submit_wrapper = "d-grid mt-1"
            form_class = "d-flex flex-column gap-2"
        else:
            email_class = "form-control"
            submit_class = "btn btn-primary"
            email_wrapper = "mb-3"
            submit_wrapper = None
            form_class = None

        self.fields["email"].widget.attrs["class"] = email_class

        self.helper = FormHelper(self)
        self.helper.form_id = form_id
        self.helper.form_method = "post"
        self.helper.form_action = reverse("marketing:beta_waitlist")
        self.helper.include_media = False
        if form_class:
            self.helper.form_class = form_class
        self.helper.attrs = {
            "hx-post": self.helper.form_action,
            "hx-target": f"#{resolved_target}",
            "hx-swap": "outerHTML",
            "novalidate": "novalidate",
        }

        submit = Submit("submit", _("Notify Me"), css_class=submit_class)
        if submit_wrapper:
            submit.wrapper_class = submit_wrapper

        self.helper.layout = Layout(
            Field("email", wrapper_class=email_wrapper),
            Field("company", type="hidden", wrapper_class="d-none"),
            Field("origin", type="hidden", wrapper_class="d-none"),
            Div(
                submit,
            ),
        )
