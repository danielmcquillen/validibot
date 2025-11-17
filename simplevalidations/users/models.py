from __future__ import annotations

from uuid import uuid4

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import CharField
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from simplevalidations.users.constants import RoleCode


def _workspace_name_for(user: "User") -> str:
    source = (user.name or "").strip() or (user.username or "Workspace")
    if source.endswith("s"):
        return f"{source}’ Workspace"
    return f"{source}’s Workspace"


def _generate_unique_slug(model, base: str, *, prefix: str = "") -> str:
    base_slug = slugify(base) or uuid4().hex[:10]
    if prefix:
        base_slug = f"{prefix}{base_slug}"
    slug = base_slug
    counter = 2
    while model.objects.filter(slug=slug).exists():
        slug = f"{base_slug}-{counter}"
        counter += 1
    return slug


def ensure_default_project(organization: "Organization"):
    from simplevalidations.projects.models import Project

    default = Project.all_objects.filter(org=organization, is_default=True).first()
    if default:
        if not default.is_active:
            default.is_active = True
            default.deleted_at = None
            default.save(update_fields=["is_active", "deleted_at"])
        return default

    name = _("Default Project")
    slug = _generate_unique_slug(Project, name, prefix="default-")
    return Project.all_objects.create(
        org=organization,
        name=name,
        description="",
        slug=slug,
        is_default=True,
        is_active=True,
        color=Project.DEFAULT_BADGE_COLOR,
    )


def ensure_personal_workspace(user: "User") -> "Organization":
    existing = (
        user.orgs.filter(is_personal=True, membership__is_active=True)
        .distinct()
        .first()
    )
    if existing:
        ensure_default_project(existing)
        if not user.current_org_id:
            user.set_current_org(existing)
        return existing

    name = _workspace_name_for(user)
    slug = _generate_unique_slug(Organization, name, prefix="workspace-")
    personal_org = Organization.objects.create(
        name=name,
        slug=slug,
        is_personal=True,
    )
    membership = Membership.objects.create(
        user=user,
        org=personal_org,
        is_active=True,
    )
    membership.set_roles({RoleCode.ADMIN, RoleCode.OWNER, RoleCode.EXECUTOR})
    ensure_default_project(personal_org)
    user.set_current_org(personal_org)
    return personal_org


class Role(models.Model):
    """
    Global catalog of roles (e.g., OWNER, ADMIN, MEMBER, VIEWER).
    """

    code = models.CharField(
        max_length=32,
        choices=RoleCode.choices,
        default=RoleCode.WORKFLOW_VIEWER,
    )

    name = models.CharField(max_length=64)  # display name

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code


class Organization(TimeStampedModel):
    """
    Model to represent an organization that can have multiple users.
    """

    name = CharField(
        max_length=255,
        unique=False,
        blank=False,
        help_text=_("Name of the organization, e.g. 'My Organization'"),
    )

    slug = models.SlugField(
        unique=True,
        blank=True,
        null=False,
    )  # e.g. "my-organization"

    is_personal = models.BooleanField(
        default=False,
        help_text=_(
            "Indicates if this organization is a personal workspace for a user.",
        ),
    )

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        """Override save to ensure slug is set if not provided."""
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        """Get URL for organization's detail view.

        Returns:
            str: URL for organization detail.

        """
        return reverse("organizations:detail", kwargs={"pk": self.pk})

    def delete(self, *args, **kwargs):  # noqa: D401 - guard deletion of personal orgs
        if self.is_personal:
            raise ValidationError("Personal organizations cannot be deleted.")
        super().delete(*args, **kwargs)


class User(AbstractUser):
    """
    Default custom user model for SimpleValidations.
    If adding fields that need to be filled at user signup,
    check forms.SignupForm and forms.SocialSignupForms accordingly.
    """

    # First and last name do not cover name patterns around the globe
    name = CharField(_("Name of User"), blank=True, max_length=255)

    avatar = models.ImageField(
        upload_to="avatars/",
        blank=True,
        null=True,
        help_text=_("Square image works best across the app."),
    )
    job_title = models.CharField(
        _("Job title"),
        max_length=128,
        blank=True,
        default="",
    )
    company = models.CharField(
        _("Company"),
        max_length=255,
        blank=True,
        default="",
    )
    location = models.CharField(
        _("Location"),
        max_length=255,
        blank=True,
        default="",
    )
    timezone = models.CharField(
        _("Timezone"),
        max_length=64,
        blank=True,
        default="",
    )
    bio = models.TextField(
        _("Bio"),
        blank=True,
        default="",
    )

    first_name = None  # type: ignore[assignment]

    last_name = None  # type: ignore[assignment]

    # Many-to-many relationship with Organization through Membership
    orgs = models.ManyToManyField(
        "Organization",
        through="Membership",
        related_name="users",
        blank=True,
    )

    # Points to the organization the user is currently "scoped" to in the UI.
    # Nullable because a brand-new user may not have picked/created one yet.
    current_org = models.ForeignKey(
        "Organization",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="current_users",
        help_text=_(
            "Organization the user is currently working in (can be changed by user).",
        ),
    )

    def get_current_org(self) -> Organization | None:
        """
        Return the current_org (cached via select_related in callers).
        If one isn't defined, set it to the user's personal org if it exists.
        If no personal org exists, create one and set it.
        """
        if self.current_org and Membership.objects.filter(
            user=self,
            org=self.current_org,
            is_active=True,
        ).exists():
            return self.current_org

        return ensure_personal_workspace(self)

    def set_current_org(
        self,
        orgs: Organization,
        *,
        save: bool = True,
    ):
        """
        Assign current_org ensuring the user is a member of it.

        Args:
            organization: Organization instance to scope the user to.
            save: Persist immediately (default True).

        Raises:
            ValueError: If the user is not a member of the organization.
        """
        if not orgs:
            msg = "Organization cannot be None when calling set_current_org()."
            raise ValueError(msg)

        if self.current_org and self.current_org == orgs:
            return

        if not self.orgs.filter(
            id=orgs.id,
            membership__is_active=True,
        ).exists():
            msg = (
                "User must be an active member of the organization "
                "to set it as current."
            )
            raise ValueError(msg)

        self.current_org = orgs

        if save:
            self.save(update_fields=["current_org"])

    def membership_for_current_org(self) -> Membership | None:
        """
        Return the Membership object for current_org
        (cached via select_related in callers).
        """
        if not self.current_org:
            return None
        return Membership.objects.filter(user=self, org=self.current_org).first()

    def has_org_roles(self, org: Organization, role_codes: set[str]) -> bool:
        """
        Return True when the user is an active member of ``org`` with any of the
        roles in ``role_codes``.
        """

        if not org:
            return False

        membership = (
            self.memberships.filter(org=org, is_active=True)
            .select_related("org")
            .prefetch_related("membership_roles__role")
            .first()
        )
        if membership is None:
            return False
        return membership.has_any_role(role_codes)

    def get_absolute_url(self) -> str:
        """Get URL for user's detail view.

        Returns:
            str: URL for user detail.

        """
        return reverse("users:detail", kwargs={"username": self.username})


class Membership(TimeStampedModel):
    """
    Many-to-many through table. A user can belong to multiple orgs with roles.
    """

    class Meta:
        unique_together = [
            (
                "user",
                "org",
            ),
        ]  # prevent dup memberships
        indexes = [
            models.Index(
                fields=[
                    "org",
                    "user",
                ],
            ),
        ]

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="memberships",
    )

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
    )

    roles = models.ManyToManyField(
        Role,
        through="MembershipRole",
        related_name="memberships",
        blank=True,
    )

    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"user '{self.user.username}' in org '{self.org.name}'"

    @property
    def joined_at(self):
        """Get the date when the user joined the organization."""
        return self.created

    def has_role(self, role_code: str) -> bool:
        return self.roles.filter(code=role_code).exists()

    def has_any_role(self, role_codes: set[str]) -> bool:
        """
        Return True when membership includes any role in ``role_codes``.
        """

        codes = self.role_codes
        return any(code in codes for code in role_codes)

    @property
    def role_codes(self) -> set[str]:
        return set(self.roles.values_list("code", flat=True))

    @property
    def is_admin(self) -> bool:
        from simplevalidations.users.constants import RoleCode

        return self.has_role(RoleCode.ADMIN) or self.has_role(RoleCode.OWNER)

    @property
    def role_labels(self) -> list[str]:
        return list(self.roles.values_list("name", flat=True))

    @property
    def has_author_admin_owner_privileges(self) -> bool:
        """
        Return True when the membership can access author/admin experiences.
        """

        from simplevalidations.users.constants import RoleCode

        return bool(
            self.is_admin
            or self.has_role(RoleCode.AUTHOR)
            or self.has_role(RoleCode.OWNER)
        )

    def _demote_other_owners(self):
        if not self.org_id:
            return
        other_owner_memberships = (
            Membership.objects.filter(
                org_id=self.org_id,
                is_active=True,
                membership_roles__role__code=RoleCode.OWNER,
            )
            .exclude(pk=self.pk)
            .distinct()
        )
        for other in other_owner_memberships:
            remaining_codes = set(other.role_codes)
            if RoleCode.OWNER not in remaining_codes:
                continue
            remaining_codes.discard(RoleCode.OWNER)
            other.set_roles(remaining_codes or {RoleCode.ADMIN})

    def add_role(self, role_code: str):
        if role_code not in RoleCode.values:
            raise ValueError(f"Invalid role code: {role_code}")
        updated_codes = set(self.role_codes)
        updated_codes.add(role_code)
        self.set_roles(updated_codes)

    def set_roles(self, role_codes: list[str] | set[str]):
        normalized_codes = {code for code in role_codes if code in RoleCode.values}
        if not normalized_codes:
            normalized_codes = {RoleCode.WORKFLOW_VIEWER}
        requested_owner = RoleCode.OWNER in normalized_codes
        if requested_owner:
            normalized_codes = set(RoleCode.values)
        if requested_owner:
            self._demote_other_owners()
        roles = list(Role.objects.filter(code__in=normalized_codes))
        if len(normalized_codes) != len(roles):
            missing = normalized_codes - {role.code for role in roles}
            for code in missing:
                role, _ = Role.objects.get_or_create(
                    code=code,
                    defaults={
                        "name": getattr(RoleCode, code).label
                        if hasattr(RoleCode, code)
                        else code.title(),
                    },
                )
                roles.append(role)

        self.membership_roles.all().delete()
        for role in roles:
            MembershipRole.objects.get_or_create(membership=self, role=role)

    def remove_role(self, role_code: str):
        updated_codes = set(self.role_codes)
        updated_codes.discard(role_code)
        self.set_roles(updated_codes)


class MembershipRole(models.Model):
    """
    Through model allowing multiple roles per membership.
    """

    membership = models.ForeignKey(
        Membership,
        on_delete=models.CASCADE,
        related_name="membership_roles",
    )

    role = models.ForeignKey(
        Role,
        on_delete=models.CASCADE,
        related_name="membership_roles",
    )

    class Meta:
        unique_together = [
            (
                "membership",
                "role",
            ),
        ]
        indexes = [
            models.Index(
                fields=[
                    "membership",
                    "role",
                ],
            ),
        ]

    def __str__(self):
        return f"{self.membership_id}:{self.role.code}"
