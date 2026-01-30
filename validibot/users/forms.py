from datetime import timedelta

from allauth.account.forms import LoginForm
from allauth.account.forms import SignupForm
from allauth.socialaccount.forms import SignupForm as SocialSignupForm
from crispy_forms.helper import FormHelper
from django import forms
from django.conf import settings
from django.contrib.auth import forms as admin_forms
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from validibot.users.constants import RESERVED_ORG_SLUGS
from validibot.users.constants import RoleCode
from validibot.users.models import MemberInvite
from validibot.users.models import Membership
from validibot.users.models import Organization
from validibot.users.models import User

ROLE_HELP_TEXT: dict[str, str] = {
    RoleCode.OWNER: _(
        "ASSIGNED AT SETUP AND CANNOT BE CHANGED HERE. "
        "Sole org authority with all admin rights."
    ),
    RoleCode.ADMIN: _(
        "Includes Author, Executor, Validation Results Viewer, "
        "Analytics Viewer, and Workflow Viewer. Uncheck Admin to "
        "fine-tune individual permissions."
    ),
    RoleCode.AUTHOR: _(
        "Includes Executor, Validation Results Viewer, "
        "Analytics Viewer, and Workflow Viewer capabilities plus creating "
        "and editing workflows, validators, and rulesets."
    ),
    RoleCode.EXECUTOR: _(
        "Includes Workflow Viewer access plus launch workflows, "
        "monitor progress, and review the runs they launch."
    ),
    RoleCode.ANALYTICS_VIEWER: _(
        "Read-only access to analytics dashboards and reports."
    ),
    RoleCode.VALIDATION_RESULTS_VIEWER: _(
        "Read-only access to all validation runs in organization."
    ),
    RoleCode.WORKFLOW_VIEWER: _(
        "Read-only access to workflows and org reports "
        "(no edit or execution permissions)."
    ),
}

ROLE_IMPLICATIONS: dict[str, set[str]] = {
    RoleCode.OWNER: set(RoleCode.values),
    RoleCode.ADMIN: {
        RoleCode.AUTHOR,
        RoleCode.EXECUTOR,
        RoleCode.VALIDATION_RESULTS_VIEWER,
        RoleCode.ANALYTICS_VIEWER,
        RoleCode.WORKFLOW_VIEWER,
    },
    RoleCode.AUTHOR: {
        RoleCode.EXECUTOR,
        RoleCode.ANALYTICS_VIEWER,
        RoleCode.VALIDATION_RESULTS_VIEWER,
        RoleCode.WORKFLOW_VIEWER,
    },
    RoleCode.EXECUTOR: {
        RoleCode.WORKFLOW_VIEWER,
    },
}


def _minimize_roles(role_codes: set[str]) -> set[str]:
    """
    Reduce a set of roles to the minimal set of explicit selections.

    For example, if input is {ADMIN, AUTHOR, EXECUTOR, WORKFLOW_VIEWER},
    return {ADMIN} because ADMIN implies all the others.
    """
    minimal = set(role_codes)

    # Remove any role that's implied by another role in the set
    for role in role_codes:
        for grant in ROLE_IMPLICATIONS.get(role, ()):
            minimal.discard(grant)

    return minimal


def _build_role_options(
    selected_roles: set[str],
    *,
    owner_locked: bool = False,
    disable_owner_checkbox: bool = True,
    implied_roles: set[str] | None = None,
) -> list[dict[str, str | bool]]:
    """
    Prepare a template-friendly list describing each role option.
    """

    implied_roles = implied_roles or set()
    options: list[dict[str, str | bool]] = []
    for code, label in RoleCode.choices:
        is_implied = code in implied_roles
        option = {
            "value": code,
            "label": label,
            "help": ROLE_HELP_TEXT.get(code, ""),
            "checked": code in selected_roles,
            "disabled": owner_locked
            or (disable_owner_checkbox and code == RoleCode.OWNER)
            or is_implied,
            "implied": is_implied,
        }
        options.append(option)
    return options


def _expand_roles_with_implications(role_codes: set[str]) -> tuple[set[str], set[str]]:
    """
    Expand role selections with implied roles (e.g., Admin -> Author/Executor).
    Returns the expanded set plus the subset that were implied (automatically granted
    by higher roles), excluding any roles that were explicitly in the input set.
    """

    expanded = set(role_codes)
    implied: set[str] = set()

    # For each explicitly selected role, add its implications
    for role in role_codes:
        for grant in ROLE_IMPLICATIONS.get(role, ()):
            expanded.add(grant)
            # A role is implied only if it wasn't explicitly selected
            if grant not in role_codes:
                implied.add(grant)

    return expanded, implied


def _extract_role_values(source, key: str) -> list[str]:
    """
    Normalize bound data access so tests and QueryDict behave the same.
    """

    if hasattr(source, "getlist"):
        return list(source.getlist(key))
    value = source.get(key)
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


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
        self.user: User | None = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_tag = False

    def clean_name(self):
        """Validate that reserved slugs are only used by superusers."""
        from django.utils.text import slugify

        name = self.cleaned_data.get("name", "")
        slug = slugify(name)

        if slug in RESERVED_ORG_SLUGS:
            is_superuser = self.user and self.user.is_superuser
            if not is_superuser:
                raise forms.ValidationError(
                    _("This organization name is reserved. Please choose another."),
                )
        return name


class OrganizationMemberForm(forms.Form):
    email = forms.EmailField(label=_("User email"))
    roles = forms.MultipleChoiceField(
        label=_("Roles"),
        required=False,
        choices=RoleCode.choices,
        widget=forms.CheckboxSelectMultiple,
        initial=[RoleCode.WORKFLOW_VIEWER],
    )

    def __init__(self, *args, **kwargs):
        self.organization: Organization | None = kwargs.pop("organization", None)
        self.request_user = kwargs.pop("request_user", None)
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_tag = False
        self.assignable_role_codes = {
            code for code, _ in RoleCode.choices if code != RoleCode.OWNER
        }
        assignable_choices = [
            (code, label)
            for code, label in RoleCode.choices
            if code in self.assignable_role_codes
        ]
        self.fields["roles"].choices = assignable_choices
        self.fields["roles"].help_text = _(
            "Invitees may become Admins, Authors, Executors, or Viewers. "
            "The Owner role is fixed and cannot be granted through this form."
        )
        self.fields["email"].widget.attrs.setdefault("class", "form-control")

        if self.organization:
            self.fields["email"].help_text = _(
                "Enter the email address of an existing user to add them to %(org)s."
            ) % {"org": self.organization.name}

        if self.is_bound:
            selected_roles = set(_extract_role_values(self.data, "roles"))
        else:
            selected_roles = (
                self.initial.get("roles", self.fields["roles"].initial) or []
            )
            if isinstance(selected_roles, str):
                selected_roles = [selected_roles]
            selected_roles = set(selected_roles)
        selected_roles &= self.assignable_role_codes
        selected_roles, implied_roles = _expand_roles_with_implications(selected_roles)
        self.role_options = _build_role_options(
            selected_roles,
            owner_locked=False,
            disable_owner_checkbox=True,
            implied_roles=implied_roles,
        )

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        try:
            self.user = User.objects.get(email__iexact=email)
        except User.DoesNotExist as exc:  # pragma: no cover - guard
            raise forms.ValidationError(
                _("No user with that email exists in Validibot."),
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

    def clean_roles(self):
        roles = self.cleaned_data.get("roles") or []
        roles_set = {role for role in roles if role in self.assignable_role_codes}
        extra = (
            set(_extract_role_values(self.data, "roles")) - self.assignable_role_codes
        )
        if extra:
            raise forms.ValidationError(
                _("Owner role cannot be assigned through this form.")
            )
        expanded, implied_roles = _expand_roles_with_implications(roles_set)
        return list(expanded)

    def save(self) -> Membership:
        roles = self.cleaned_data.get("roles") or [RoleCode.WORKFLOW_VIEWER]
        membership = Membership.objects.create(
            user=self.user,
            org=self.organization,
            is_active=True,
        )
        membership.set_roles(roles)
        return membership


class InviteUserForm(forms.Form):
    """Form to send an invitation to an existing user or email."""

    search = forms.CharField(
        label=_("User or email"),
        required=True,
        help_text=_("Start typing a username or email to search."),
    )
    invitee_user = forms.IntegerField(required=False, widget=forms.HiddenInput())
    invitee_email = forms.EmailField(required=False, widget=forms.HiddenInput())
    roles = forms.MultipleChoiceField(
        label=_("Roles"),
        required=False,
        choices=RoleCode.choices,
        widget=forms.CheckboxSelectMultiple,
        initial=[RoleCode.WORKFLOW_VIEWER],
    )

    def __init__(self, *args, **kwargs):
        self.organization: Organization | None = kwargs.pop("organization", None)
        self.inviter: User | None = kwargs.pop("inviter", None)
        super().__init__(*args, **kwargs)
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_tag = False
        self.fields["roles"].choices = RoleCode.choices
        if self.is_bound:
            selected_roles = set(_extract_role_values(self.data, "roles"))
        else:
            selected_roles = set(self.fields["roles"].initial or [])
        self.role_options = _build_role_options(
            selected_roles,
            owner_locked=False,
            disable_owner_checkbox=True,
        )

    def clean(self):
        cleaned = super().clean()
        if self.organization is None or self.inviter is None:
            raise forms.ValidationError(_("Organization context is required."))

        user_id = cleaned.get("invitee_user")
        email = cleaned.get("invitee_email") or cleaned.get("search")
        if not user_id and not email:
            raise forms.ValidationError(_("Select a user or provide an email."))
        invitee_user = None
        if user_id:
            try:
                invitee_user = User.objects.get(pk=user_id)
            except User.DoesNotExist as exc:
                raise forms.ValidationError(_("Selected user does not exist.")) from exc
            cleaned["invitee_user"] = invitee_user
            cleaned["invitee_email"] = invitee_user.email
        else:
            cleaned["invitee_user"] = None
            cleaned["invitee_email"] = email
        return cleaned

    def save(self) -> MemberInvite:
        roles = self.cleaned_data.get("roles") or [RoleCode.WORKFLOW_VIEWER]
        invitee_user = self.cleaned_data.get("invitee_user")
        invitee_email = self.cleaned_data.get("invitee_email")
        # Email is only sent if invitee is NOT already a registered user
        # (registered users receive in-app notifications instead)
        invite = MemberInvite.create_with_expiry(
            org=self.organization,
            inviter=self.inviter,
            invitee_user=invitee_user,
            invitee_email=invitee_email,
            roles=roles,
            expires_at=timezone.now() + timedelta(days=7),
            send_email=(invitee_user is None),
        )
        return invite


class OrganizationMemberRolesForm(forms.Form):
    """

    A form for selecting the roles assigned to an organization member.

    The following rules should be followed:

    - OWNER role is never enabled.
    - If ADMIN is selected, all other roles checkboxes are selected and disabled
      for further edits.
    - If AUTHOR is selected, EXECUTOR, ANALYTICS_VIEWER, VALIDATION_RESULTS_VIEWER,
      and WORKFLOW_VIEWER are checkboxes selected and disabled for further edits.
    - If EXECUTOR is selected, WORKFLOW_VIEWER checkbox is selected and disabled,
      while the ANALYTICS_VIEWER and VALIDATION_RESULTS_VIEWER checkboxes are
      enabled for further edits.
    - ANALYTICS_VIEWER and VALIDATION_RESULTS_VIEWER can be selected/deselected
      independently unless disabled by the above rules.


    IMPORTANT: When the form is first shown, the above rules should be applied to
    reflect the current state of the member's roles when the form is initialized.

    When the form is submitted, the above rules should be enforced in the
    clean_roles method.

    Args:
        forms (_type_): _description_

    Raises:
        forms.ValidationError: _description_
        forms.ValidationError: _description_

    Returns:
        _type_: _description_
    """

    roles = forms.MultipleChoiceField(
        label=_("Roles"),
        required=False,
        choices=RoleCode.choices,
        widget=forms.CheckboxSelectMultiple,
    )

    def __init__(self, *args, **kwargs):
        self.membership: Membership = kwargs.pop("membership")
        super().__init__(*args, **kwargs)
        current_roles = set(self.membership.role_codes)
        owner_locked = RoleCode.OWNER in current_roles
        if owner_locked:
            current_roles = set(RoleCode.values)
        if self.is_bound and not owner_locked:
            bound_roles = set(_extract_role_values(self.data, "roles"))
            valid_codes = {code for code, _ in RoleCode.choices}
            current_roles = bound_roles & valid_codes
        self.fields["roles"].choices = RoleCode.choices
        self.fields["roles"].initial = list(current_roles)
        # Minimize to find explicit selections, then re-expand to get implied roles
        minimal_roles = _minimize_roles(current_roles)
        expanded_roles, implied_roles = _expand_roles_with_implications(minimal_roles)
        self.role_options = _build_role_options(
            expanded_roles,
            owner_locked=owner_locked,
            disable_owner_checkbox=True,
            implied_roles=implied_roles,
        )
        self.owner_locked = owner_locked

        self.disable_all_roles = owner_locked
        self.helper = FormHelper()
        self.helper.form_method = "post"
        self.helper.form_tag = False

    def clean_roles(self):
        roles = set(self.cleaned_data.get("roles") or [])
        valid_codes = {code for code, _ in RoleCode.choices}
        normalized = {role for role in roles if role in valid_codes}
        if self.owner_locked:
            # Preserve owner regardless of bound data; the checkbox is disabled.
            normalized.add(RoleCode.OWNER)
        if RoleCode.OWNER in normalized and not self.owner_locked:
            raise forms.ValidationError(
                _("The Owner role cannot be assigned through this screen."),
            )

        # Enforce cascading role rules
        if RoleCode.ADMIN in normalized:
            normalized.update(set(RoleCode.values) - {RoleCode.OWNER})
        if RoleCode.AUTHOR in normalized:
            normalized.update(
                {
                    RoleCode.EXECUTOR,
                    RoleCode.ANALYTICS_VIEWER,
                    RoleCode.VALIDATION_RESULTS_VIEWER,
                    RoleCode.WORKFLOW_VIEWER,
                }
            )
        if RoleCode.EXECUTOR in normalized:
            normalized.add(RoleCode.WORKFLOW_VIEWER)

        return list(normalized)

    def save(self) -> Membership:
        roles = set(self.cleaned_data.get("roles") or [])
        if self.owner_locked:
            roles.update(RoleCode.values)
            roles.add(RoleCode.OWNER)
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


def _recaptcha_enabled() -> bool:
    """Check if reCAPTCHA is configured (both keys must be set)."""
    return bool(
        getattr(settings, "RECAPTCHA_PUBLIC_KEY", "")
        and getattr(settings, "RECAPTCHA_PRIVATE_KEY", "")
    )


class UserSignupForm(SignupForm):
    """
    Form that will be rendered on a user sign up section/screen.
    Default fields will be added automatically.
    Check UserSocialSignupForm for accounts created from social.
    """

    terms_accepted = forms.BooleanField(
        required=True,
        label=_("I agree to the Terms of Service and Privacy Policy"),
        error_messages={
            "required": _("You must agree to the Terms of Service and Privacy Policy."),
        },
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only add reCAPTCHA field if keys are configured
        if _recaptcha_enabled():
            from django_recaptcha.fields import ReCaptchaField
            from django_recaptcha.widgets import ReCaptchaV3

            self.fields["captcha"] = ReCaptchaField(widget=ReCaptchaV3(action="signup"))


class UserLoginForm(LoginForm):
    """
    Custom login form with optional reCAPTCHA support.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only add reCAPTCHA field if keys are configured
        if _recaptcha_enabled():
            from django_recaptcha.fields import ReCaptchaField
            from django_recaptcha.widgets import ReCaptchaV3

            self.fields["captcha"] = ReCaptchaField(widget=ReCaptchaV3(action="login"))


class UserSocialSignupForm(SocialSignupForm):
    """
    Renders the form when user has signed up using social accounts.
    Default fields will be added automatically.
    See UserSignupForm otherwise.
    """
