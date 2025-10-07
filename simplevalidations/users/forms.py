from allauth.account.forms import SignupForm
from allauth.socialaccount.forms import SignupForm as SocialSignupForm
from django import forms
from django.contrib.auth import forms as admin_forms
from django.utils.translation import gettext_lazy as _

from .models import User


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
