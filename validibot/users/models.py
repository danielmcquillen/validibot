from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.files.storage import storages
from django.db import models
from django.db.models import CharField
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from validibot.users.constants import RESERVED_ORG_SLUGS
from validibot.users.constants import RoleCode


def select_public_storage():
    """Return the explicitly public storage backend for public profile media."""
    try:
        return storages["public"]
    except Exception:
        return storages["default"]


def _workspace_name_for(user: User) -> str:
    source = (user.name or "").strip() or (user.username or "Workspace")
    if source.endswith("s"):
        return f"{source}' Workspace"
    return f"{source}'s Workspace"


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


def ensure_default_project(organization: Organization):
    from validibot.projects.models import Project

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


def ensure_personal_workspace(
    user: User,
    *,
    force: bool = False,
) -> Organization | None:
    """
    Ensure the user has a personal workspace organization.

    If the user already has a personal workspace, returns it.
    Otherwise, creates a new organization with:
    - A membership for the user (owner/admin roles)
    - A default project
    - A subscription on the Starter plan with 14-day trial

    Returns ``None`` in two cases (both bypassed when ``force=True``):

    * **GUEST-classified accounts**: a guest's classification is
      sticky and ``Membership.clean`` would block any ``Membership``
      creation anyway. Returning early avoids tripping that guard
      during normal request processing (e.g. the context processor
      calling ``get_current_org`` on a logged-in guest).

    * **Legacy workflow-guest predicate**: users with active grants
      and no memberships. Preserved so deployments without sticky
      semantics retain their established behaviour — a freshly-invited
      guest in community lands here and stays workspace-less.

    The ``force=True`` flag is for the ``promote_user`` flow. After a
    user is promoted from GUEST to BASIC, the previous-state predicates
    don't apply — the call site has just changed the user's
    classification and needs a workspace provisioned regardless of
    grants or prior membership state. Without ``force`` the legacy
    predicate would short-circuit and strand the newly-promoted user
    with no operational org.

    Guest accounts (when not being force-promoted) operate without a
    personal workspace; their usage is billed to the workflow owner's
    org.
    """

    if not force:
        # Sticky GUEST kind takes precedence — provisioning a personal
        # workspace for a guest would fail at ``Membership.clean`` anyway.
        from validibot.users.constants import UserKindGroup

        if user.user_kind == UserKindGroup.GUEST:
            return None

        # Legacy workflow-guest predicate (community deployments retain
        # their pre-sticky behaviour). Import here to avoid circular import.
        from validibot.workflows.models import WorkflowAccessGrant

        has_memberships = user.memberships.filter(is_active=True).exists()
        has_grants = WorkflowAccessGrant.objects.filter(
            user=user,
            is_active=True,
        ).exists()

        if has_grants and not has_memberships:
            # Legacy workflow-guest path — no personal workspace needed.
            return None

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
    # Slug is derived from the username, not the display name, so personal-org
    # URLs look like /orgs/danielmctest/ rather than /orgs/danielmctests-workspace/.
    # The display name keeps the "'s Workspace" suffix for UI clarity.
    slug_source = (user.username or "").strip() or name
    slug = _generate_unique_slug(Organization, slug_source)
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

    # Trial fields — populated by the cloud onboarding layer when a user
    # accepts a trial invite. Community (self-hosted) users never see these
    # populated. Both are nullable so community orgs are unaffected.
    trial_ends_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_(
            "When this organization's trial expires. "
            "NULL means no trial (community/self-hosted or paid subscription)."
        ),
    )
    trial_duration_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_(
            "The trial duration in days, snapshotted "
            "when the trial was activated. NULL for non-trial orgs."
        ),
    )

    # Set this to True to bypass reserved slug validation (for superuser/admin use)
    _allow_reserved_slug: bool = False

    def __str__(self):
        return self.name

    def clean(self):
        """Validate organization data."""
        super().clean()
        # Auto-generate slug for validation if not set
        slug = self.slug or slugify(self.name)
        if slug in RESERVED_ORG_SLUGS and not self._allow_reserved_slug:
            raise ValidationError(
                {"name": _("This organization name is reserved.")},
            )

    def save(self, *args, **kwargs):
        """Override save to ensure slug is set if not provided."""
        if not self.slug:
            candidate = slugify(self.name) if self.name else ""
            if not candidate:
                # Fallback for names that don't slugify (e.g., only punctuation/unicode)
                candidate = f"org-{uuid4().hex[:8]}"
            self.slug = candidate
        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        """Get URL for organization's detail view.

        Returns:
            str: URL for organization detail.

        """
        return reverse("organizations:detail", kwargs={"pk": self.pk})

    def delete(self, *args, **kwargs):
        if self.is_personal:
            raise ValidationError("Personal organizations cannot be deleted.")
        super().delete(*args, **kwargs)


class User(AbstractUser):
    """
    Default custom user model for Validibot.
    If adding fields that need to be filled at user signup,
    check forms.SignupForm and forms.SocialSignupForms accordingly.
    """

    # First and last name do not cover name patterns around the globe
    name = CharField(_("Name of User"), blank=True, max_length=255)

    avatar = models.ImageField(
        upload_to="avatars/",
        blank=True,
        null=True,
        storage=select_public_storage,
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

        If current_org is defined and the user has an active membership, return it.
        Otherwise, try to create/return the user's personal workspace.

        Returns None for Workflow Guests (users with workflow grants but no
        org memberships). Guests operate without an organization context.
        """
        if (
            self.current_org
            and Membership.objects.filter(
                user=self,
                org=self.current_org,
                is_active=True,
            ).exists()
        ):
            return self.current_org

        # This returns None for workflow guests
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

    @property
    def user_kind(self):
        """Return the system-wide account classification.

        Returns a :class:`~validibot.users.constants.UserKindGroup` value:

        * ``UserKindGroup.GUEST`` — account is in the ``Guests`` Django
          Group. The classifier is sticky: it only changes when a
          superuser explicitly runs the ``promote_user`` management
          command (or the matching admin action). Pro deployments only.
        * ``UserKindGroup.BASIC`` — every other case. In community
          deployments (no ``guest_management`` Pro feature), every user
          is BASIC; there is no GUEST classification without Pro.

        This is a SYSTEM-WIDE property of the account, not a workflow-
        specific one. To ask "does this user have guest access to a
        specific workflow?", use the per-workflow grant machinery
        (:class:`~validibot.workflows.models.WorkflowAccessGrant`,
        :meth:`~validibot.workflows.models.Workflow.can_view`) — a BASIC
        user can hold a ``WorkflowAccessGrant`` for cross-org workflow
        sharing without becoming a GUEST.
        """

        from validibot.core.features import CommercialFeature
        from validibot.core.features import is_feature_enabled
        from validibot.users.constants import UserKindGroup

        if not is_feature_enabled(CommercialFeature.GUEST_MANAGEMENT):
            return UserKindGroup.BASIC

        if self.groups.filter(name=UserKindGroup.GUEST.value).exists():
            return UserKindGroup.GUEST
        return UserKindGroup.BASIC

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

    def clean(self):
        """Block adding GUEST-classified users as organization members.

        Sticky guest semantics: a user whose system-wide
        :attr:`~validibot.users.models.User.user_kind` is ``GUEST``
        cannot be promoted to an org member silently. The sanctioned
        path is the ``promote_user`` management command (or its admin-
        action wrapper), which moves the user from ``Guests`` to
        ``Basic Users`` first, then optionally creates a personal org
        membership in one audited transaction.

        Gated on the ``guest_management`` Pro feature: in community
        deployments the GUEST classification doesn't exist, so the
        guard is a no-op there. Within Pro it is the data-layer safety
        net that catches direct ``Membership.objects.create(...)``
        calls, fixtures, admin shortcuts, and any future code path
        that would otherwise quietly upgrade a guest's authority.
        """

        super().clean()

        from validibot.core.features import CommercialFeature
        from validibot.core.features import is_feature_enabled
        from validibot.users.constants import UserKindGroup

        if not is_feature_enabled(CommercialFeature.GUEST_MANAGEMENT):
            return

        if not self.user_id:
            return

        if self.user.user_kind == UserKindGroup.GUEST:
            raise ValidationError(
                _(
                    "Cannot add a guest user as an organization member. "
                    "Run 'manage.py promote_user --email <email> --to basic' "
                    "first, then create the membership.",
                ),
            )

    def save(self, *args, **kwargs):
        """Enforce :meth:`clean` on every write path.

        Django's ``ModelForm`` runs ``full_clean`` automatically, but
        direct ``Membership.objects.create`` calls and bulk shortcuts
        do not. Routing every ``save`` through ``full_clean`` ensures
        the GUEST-as-member guard fires on the data path too — without
        this the guard is just a UI-form validator.
        """

        self.full_clean()
        super().save(*args, **kwargs)

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
        from validibot.users.constants import RoleCode

        return self.has_role(RoleCode.ADMIN) or self.has_role(RoleCode.OWNER)

    @property
    def role_labels(self) -> list[str]:
        return list(self.roles.values_list("name", flat=True))

    @property
    def has_author_admin_owner_privileges(self) -> bool:
        """
        Return True when the membership can access author/admin experiences.
        """

        from validibot.users.constants import RoleCode

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


class MemberInvite(TimeStampedModel):
    """
    Invitation to join an organization as a member with proposed roles.

    Unlike WorkflowInvite (for guest access to a single workflow), accepting
    this invite creates a full Membership with roles in the organization.

    Note: This class was formerly called PendingInvite. The name was changed
    to better distinguish membership invites from guest/workflow invites.
    """

    # Import InviteStatus from core to use shared status choices
    from validibot.core.constants import InviteStatus

    # Keep Status as alias for backward compatibility during migration
    Status = InviteStatus

    # Default invite expiry: 7 days
    DEFAULT_EXPIRY_DAYS = 7

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="member_invites",
    )
    inviter = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sent_member_invites",
    )
    invitee_user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="received_member_invites",
        null=True,
        blank=True,
    )
    invitee_email = models.EmailField(blank=True)
    roles = models.JSONField(default=list)
    status = models.CharField(
        max_length=16,
        choices=InviteStatus.choices,
        default=InviteStatus.PENDING,
    )
    expires_at = models.DateTimeField()
    token = models.UUIDField(default=uuid4, editable=False, unique=True)

    class Meta:
        ordering = ["-created"]
        verbose_name = _("member invite")
        verbose_name_plural = _("member invites")
        # Rename database table from pendinginvite to memberinvite
        db_table = "users_memberinvite"

    def __str__(self):
        target = self.invitee_user or self.invitee_email or "unknown"
        return f"Member invite to {self.org} for {target}"

    @property
    def is_expired(self) -> bool:
        """Check if invite has expired without updating status."""
        return (
            self.status == self.InviteStatus.EXPIRED
            or timezone.now() >= self.expires_at
        )

    @property
    def is_pending(self) -> bool:
        """Check if invite is still pending and not expired."""
        return self.status == self.InviteStatus.PENDING and not self.is_expired

    def mark_expired_if_needed(self) -> bool:
        """
        Check if invite has expired and update status if so.

        Returns:
            True if the invite was marked as expired, False otherwise.
        """
        if self.status != self.InviteStatus.PENDING:
            return False
        if timezone.now() >= self.expires_at:
            self.status = self.InviteStatus.EXPIRED
            self.save(update_fields=["status", "modified"])
            return True
        return False

    def accept(self, roles: list[str] | None = None) -> Membership:
        """
        Accept this invite and create a Membership.

        Args:
            roles: Optional list of role codes. If not provided, uses the
                   roles stored on the invite.

        Returns:
            The created or updated Membership.

        Raises:
            ValueError: If invite is not pending, has expired, or has no invitee_user.
        """
        if self.mark_expired_if_needed():
            raise ValueError("Invite has expired")

        if self.status != self.InviteStatus.PENDING:
            msg = f"Cannot accept invite with status {self.status}"
            raise ValueError(msg)

        if self.invitee_user is None:
            raise ValueError("Invitee user is not set; cannot accept without user.")

        membership_roles = roles or self.roles or []

        # Seat-quota gate. Skipped when the invitee is already a
        # member of this org (re-invite or role-update flow) — the
        # ``get_or_create`` below would not create a new seat in
        # that case, so it would be wrong to refuse the accept.
        already_member = Membership.objects.filter(
            user=self.invitee_user,
            org=self.org,
            is_active=True,
        ).exists()
        if not already_member:
            # Local import keeps ``users.models`` free of a hard
            # dependency on the seats module at import time —
            # avoids any chance of a cycle as the app graph loads.
            from validibot.users.seats import check_org_seat_quota

            check_org_seat_quota(self.org)

        membership, _ = Membership.objects.get_or_create(
            user=self.invitee_user,
            org=self.org,
            defaults={"is_active": True},
        )
        membership.set_roles(set(membership_roles))
        self.status = self.InviteStatus.ACCEPTED
        self.save(update_fields=["status", "modified"])

        # Clean up guest access grants now that user is a member.
        # As a member, they have broader access, so guest grants are redundant.
        self._cleanup_guest_grants()

        return membership

    def _cleanup_guest_grants(self) -> int:
        """
        Remove guest access grants for workflows in this org.

        When a user becomes a member, they no longer need individual
        WorkflowAccessGrant records for workflows in the org - they have
        access via their membership roles instead.

        Returns the count of grants deleted.
        """
        from validibot.workflows.models import WorkflowAccessGrant

        # Delete active guest grants for workflows in this org
        deleted_count, _ = WorkflowAccessGrant.objects.filter(
            user=self.invitee_user,
            workflow__org=self.org,
            is_active=True,
        ).delete()

        return deleted_count

    def decline(self) -> None:
        """Mark invite as declined."""
        if self.status != self.InviteStatus.PENDING:
            return
        self.status = self.InviteStatus.DECLINED
        self.save(update_fields=["status", "modified"])

    def cancel(self) -> None:
        """Mark invite as canceled (by inviter)."""
        if self.status != self.InviteStatus.PENDING:
            return
        self.status = self.InviteStatus.CANCELED
        self.save(update_fields=["status", "modified"])

    @classmethod
    def create_with_expiry(cls, *, send_email: bool = False, **kwargs) -> MemberInvite:
        """
        Create a new member invite with default expiry.

        Args:
            send_email: Whether to send an invitation email (default: False).
                        Email is typically only sent for non-registered users;
                        registered users receive in-app notifications instead.
            **kwargs: Fields passed to MemberInvite.objects.create()

        Returns:
            The created MemberInvite instance.
        """
        expiry = kwargs.pop(
            "expires_at",
            timezone.now() + timedelta(days=cls.DEFAULT_EXPIRY_DAYS),
        )
        invite = cls.objects.create(expires_at=expiry, **kwargs)

        if send_email:
            from validibot.workflows.emails import send_member_invite_email

            send_member_invite_email(invite)

        return invite


# Backward compatibility alias - remove after migrations
PendingInvite = MemberInvite
