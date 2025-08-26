from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models
from django.db.models import CharField
from django.urls import reverse
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from roscoe.users.constants import MemberRole


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
            "Indicates if this organization is a personal workspace for a user."
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


class User(AbstractUser):
    """
    Default custom user model for Roscoe.
    If adding fields that need to be filled at user signup,
    check forms.SignupForm and forms.SocialSignupForms accordingly.
    """

    # First and last name do not cover name patterns around the globe
    name = CharField(_("Name of User"), blank=True, max_length=255)

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
            "Organization the user is currently working in (can be changed by user)."
        ),
    )

    def get_current_org(self) -> Organization | None:
        """
        Return the current_org (cached via select_related in callers).
        If one isn't defined, set it to the user's personal org if it exists.
        If no personal org exists, create one and set it.
        """
        if self.current_org:
            return self.current_org

        personal_org = self.orgs.filter(
            is_personal=True,
            membership__is_active=True,
        ).first()
        if personal_org:
            self.set_current_org(personal_org)
            return personal_org

        # No personal org exists, create one
        personal_org = Organization.objects.create(
            name=f"{self.username}'s Personal Workspace",
            is_personal=True,
        )
        Membership.objects.create(
            user=self,
            organization=personal_org,
            role=MemberRole.OWNER,
            is_active=True,
        )
        self.set_current_org(personal_org)
        return personal_org

    def set_current_org(
        self,
        orgs: Organization,
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
            msg = "User must be an active member of the organization to set it as current."
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
        return Membership.objects.filter(user=self, orgs=self.current_org).first()

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
    )

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
    )

    role = models.CharField(
        max_length=32,
        choices=MemberRole.choices,
        default=MemberRole.MEMBER,
    )

    is_active = models.BooleanField(default=True)

    @property
    def joined_at(self):
        """Get the date when the user joined the organization."""
        return self.created
