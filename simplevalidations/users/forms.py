from allauth.account.forms import SignupForm
from allauth.socialaccount.forms import SignupForm as SocialSignupForm
from crispy_forms.helper import FormHelper
from crispy_forms.layout import Submit
from django import forms
from django.contrib.auth import forms as admin_forms
from django.utils.translation import gettext_lazy as _

from simplevalidations.users.constants import RoleCode
from simplevalidations.users.models import Membership, Organization, User


class UserProfileForm(forms.ModelForm):
    """Form used for the user profile settings page."""

    remove_avatar = forms.BooleanField(
        required=False,
        initial=False,
        label=_("Remove current avatar"),
        help_text=_("Check to delete your uploaded avatar."),
    )

    class Meta:
        model = User
        fields = [
            "avatar",
            "name",
            "username",
            "email",
            "job_title",
            "company",
            "location",
            "timezone",
            "bio",
        ]
        widgets = {
            "bio": forms.Textarea(attrs={"rows": 4}),
            "timezone": forms.TextInput(attrs={"placeholder": "e.g. America/New_York"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["email"].disabled = True
        self.fields["email"].help_text = _(
            "Manage your sign-in email from the User Email page.",
        )
        self.fields["avatar"].required = False
        self.fields["avatar"].widget.attrs.update({"accept": "image/*"})
        self.fields["username"].help_text = _(
            "This appears in shared links and invitations.",
        )
        self.fields["bio"].help_text = _(
            "Optional short bio that appears in team areas.",
        )
        for name, field in self.fields.items():
            widget = field.widget
            if name == "remove_avatar":
                widget.attrs.setdefault("class", "form-check-input")
                continue
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs.setdefault("class", "form-check-input")
            elif isinstance(widget, (forms.FileInput, forms.ClearableFileInput)):
                widget.attrs.setdefault("class", "form-control")
            else:
                base_classes = widget.attrs.get("class", "")
                widget.attrs["class"] = f"{base_classes} form-control".strip()

    def clean(self):
        cleaned_data = super().clean()
        has_new_avatar = bool(cleaned_data.get("avatar"))
        if cleaned_data.get("remove_avatar") and not has_new_avatar:
            cleaned_data["avatar"] = None
        elif has_new_avatar:
            cleaned_data["remove_avatar"] = False
        return cleaned_data

    def save(self, *, commit=True):
        user = super().save(commit=False)
        if self.cleaned_data.get("remove_avatar") and user.avatar:
            user.avatar.delete(save=False)
            user.avatar = None
        if commit:
            user.save()
            self.save_m2m()
        return user


class OrganizationForm(forms.ModelForm):
    class Meta:
        model = Organization
        fields = ["name"]
        widgets = {
            "name": forms.TextInput(
                attrs={
                    "placeholder": _("Acme Validation Lab"),
                },
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_tag = False


class OrganizationMemberForm(forms.Form):
    email = forms.EmailField(label=_("User email"))
    roles = forms.MultipleChoiceField(
        label=_("Roles"),
        required=False,
        choices=RoleCode.choices,
        widget=forms.CheckboxSelectMultiple,
        initial=[RoleCode.VIEWER],
    )

    def __init__(self, *args, **kwargs):
        self.organization: Organization | None = kwargs.pop("organization", None)
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_tag = False
        if self.organization:
            self.fields["email"].help_text = _(
                "Enter the email address of an existing user to add them to %(org)s."
            ) % {"org": self.organization.name}
        self.fields["roles"].help_text = _(
            "Selecting Owner transfers the role from the current owner and keeps them as an Admin."
        )

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        try:
            self.user = User.objects.get(email__iexact=email)
        except User.DoesNotExist as exc:  # pragma: no cover - guard
            raise forms.ValidationError(
                _("No user with that email exists in SimpleValidations."),
            ) from exc
        return email

    def clean(self):
        cleaned = super().clean()
        if self.organization is None:
            raise forms.ValidationError("Organization context is required.")

        if hasattr(self, "user"):
            existing = Membership.objects.filter(
                user=self.user,
                org=self.organization,
            ).first()
            if existing:
                raise forms.ValidationError(
                    _("That user is already a member of this organization."),
                )
        return cleaned

    def save(self) -> Membership:
        roles = self.cleaned_data.get("roles") or [RoleCode.VIEWER]
        membership = Membership.objects.create(
            user=self.user,
            org=self.organization,
            is_active=True,
        )
        membership.set_roles(roles)
        return membership


class OrganizationMemberRolesForm(forms.Form):
    roles = forms.MultipleChoiceField(
        label=_("Roles"),
        required=False,
        choices=RoleCode.choices,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, **kwargs):
        self.membership: Membership = kwargs.pop("membership")
        super().__init__(*args, **kwargs)
        self.fields["roles"].initial = list(self.membership.role_codes)
        self.fields["roles"].help_text = _(
            "Only one member can be Owner; choosing it here will move the role from the current owner."
        )
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_tag = False

    def save(self) -> Membership:
        roles = self.cleaned_data.get("roles") or []
        if not roles:
            roles = [RoleCode.VIEWER]
        self.membership.set_roles(roles)
        return self.membership


class UserAdminChangeForm(admin_forms.UserChangeForm):
    class Meta(admin_forms.UserChangeForm.Meta):  # type: ignore[name-defined]
        model = User


class UserAdminCreationForm(admin_forms.AdminUserCreationForm):
    """
    Form for User Creation in the Admin Area.
    To change user signup, see UserSignupForm and UserSocialSignupForm.
    """

    class Meta(admin_forms.UserCreationForm.Meta):  # type: ignore[name-defined]
        model = User
        error_messages = {
            "username": {"unique": _("This username has already been taken.")},
        }


class UserSignupForm(SignupForm):
    """
    Form that will be rendered on a user sign up section/screen.
    Default fields will be added automatically.
    Check UserSocialSignupForm for accounts created from social.
    """


class UserSocialSignupForm(SocialSignupForm):
    """
    Renders the form when user has signed up using social accounts.
    Default fields will be added automatically.
    See UserSignupForm otherwise.
    """
