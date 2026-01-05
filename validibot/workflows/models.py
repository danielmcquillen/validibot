from __future__ import annotations

import logging
import math
import uuid
from decimal import Decimal
from decimal import InvalidOperation
from typing import TYPE_CHECKING

from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.core.files.storage import storages
from django.db import models
from django.db import transaction
from django.db.models import Exists
from django.db.models import OuterRef
from django.db.models import Q
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from validibot.actions.models import Action
from validibot.core.mixins import FeaturedImageMixin
from validibot.core.utils import render_markdown_safe
from validibot.projects.models import Project
from validibot.submissions.constants import DataRetention
from validibot.submissions.constants import SubmissionFileType
from validibot.users.models import Membership
from validibot.users.models import Organization
from validibot.users.models import Role
from validibot.users.models import User
from validibot.users.permissions import PermissionCode
from validibot.users.permissions import roles_for_permission

if TYPE_CHECKING:
    from validibot.users.constants import RoleCode

logger = logging.getLogger(__name__)


def select_public_storage():
    """Return the public storage backend from STORAGES['public']."""
    return storages["public"]


class WorkflowQuerySet(models.QuerySet):
    """
    A custom queryset for Workflow model to add user-specific filtering methods.
    This lets us easily get workflows a user has access to based on their membership
    to organizations or via workflow access grants (for guests).
    """

    def for_user(
        self,
        user: User,
        required_role_code: RoleCode | None = None,
    ) -> WorkflowQuerySet:
        """
        Get workflows accessible to the given user.

        Access is granted via:
        1. Org membership with appropriate role (existing behavior)
        2. Being the workflow creator (existing behavior)
        3. Having an active WorkflowAccessGrant (new - for guests)

        If required_role is provided, only return workflows where the user
        has that role in the workflow's organization. In this case, guest
        grants are NOT included (role-specific queries are for org members).

        Otherwise, return all workflows the user can access via any of the
        three methods above.
        """
        if not getattr(user, "is_authenticated", False):
            return self.none()

        # Org membership subquery
        allowed_view_roles = roles_for_permission(PermissionCode.WORKFLOW_VIEW)
        membership_subq = Membership.objects.filter(
            org=OuterRef("org_id"),
            user=user,
            is_active=True,
        )
        if required_role_code:
            membership_subq = membership_subq.filter(roles__code=required_role_code)
        else:
            membership_subq = membership_subq.filter(roles__code__in=allowed_view_roles)

        # Build the access condition
        # Always include: membership access OR user is creator
        access_condition = Exists(membership_subq) | Q(user_id=user.id)

        # For non-role-specific queries, also include guest grant access
        if not required_role_code:
            grant_subq = WorkflowAccessGrant.objects.filter(
                workflow_id=OuterRef("pk"),
                user=user,
                is_active=True,
            )
            access_condition = access_condition | Exists(grant_subq)

        return (
            self.annotate(_has_access=access_condition)
            .filter(_has_access=True)
            .distinct()
        )


class WorkflowManager(models.Manager):
    def get_queryset(self):
        return WorkflowQuerySet(self.model, using=self._db)

    def for_user(self, user: User, required_role_code: RoleCode | None = None):
        return self.get_queryset().for_user(user, required_role_code=required_role_code)


def _default_workflow_file_types() -> list[str]:
    return [SubmissionFileType.JSON]


class Workflow(FeaturedImageMixin, TimeStampedModel):
    """
    Reusable, versioned definition of a sequence of validation steps.
    """

    objects = WorkflowManager()

    featured_image = models.FileField(
        null=True,
        blank=True,
        # Use public media bucket - references STORAGES["public"] from settings
        storage=select_public_storage,
        help_text=_(
            "Optional image to represent the workflow Shown on the 'info' page.",
        ),
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "org",
                    "slug",
                    "version",
                ],
                name="uq_workflow_org_slug_version",
            ),
        ]
        ordering = [
            "slug",
            "-version",
        ]

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="workflows",
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workflows",
        help_text=_(
            "Default project to associate with runs triggered from this workflow.",
        ),
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="workflows",
        help_text=_("The user who created this workflow."),
    )

    name = models.CharField(
        max_length=200,
        blank=False,
        null=False,
        help_text=_("Name of the workflow, e.g. 'My Workflow'"),
    )

    uuid = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        help_text=_("Unique identifier for the workflow."),
    )

    slug = models.SlugField(
        null=False,
        blank=True,
        help_text=_(
            "A unique identifier for the workflow, used in URLs. "
            "(Leave blank to auto-generate from name.)",
        ),
    )

    allow_submission_name = models.BooleanField(
        default=True,
        help_text=_(
            "Allow users to submit a custom name along with their data for validation.",
        ),
    )

    allow_submission_meta_data = models.BooleanField(
        default=False,
        help_text=_(
            "Allow users to submit meta-data along with their data for validation.",
        ),
    )

    allow_submission_short_description = models.BooleanField(
        default=False,
        help_text=_(
            "Allow users to submit a short description along with "
            "their data for validation.",
        ),
    )
    version = models.CharField(
        max_length=40,
        blank=True,
        default="",
    )

    is_locked = models.BooleanField(
        default=False,
    )

    is_active = models.BooleanField(
        default=True,
        help_text=_("Inactive workflows stay visible but cannot run validations."),
    )
    is_archived = models.BooleanField(
        default=False,
        help_text=_(
            "Archived workflows are disabled and hidden unless explicitly shown."
        ),
    )

    make_info_public = models.BooleanField(
        default=False,
        help_text=_(
            "Allows non-logged in users to see details of the workflow validation.",
        ),
    )

    featured_image_alt_candidates = ("name",)

    allowed_file_types = ArrayField(
        base_field=models.CharField(
            max_length=32,
            choices=SubmissionFileType.choices,
        ),
        default=_default_workflow_file_types,
        help_text=_(
            "Logical file types (JSON, XML, text, etc.) this workflow can accept.",
        ),
    )

    data_retention = models.CharField(
        max_length=32,
        choices=DataRetention.choices,
        default=DataRetention.DO_NOT_STORE,
        help_text=_(
            "How long to keep submission data after validation completes. "
            "DO_NOT_STORE queues deletion shortly after run completion."
        ),
    )

    # Methods
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def clean(self):
        if not self.name or not self.name.strip():
            raise ValidationError({"name": _("Name is required.")})
        if self.project_id and self.org_id and self.project.org_id != self.org_id:
            raise ValidationError(
                {"project": _("Project must belong to the workflow's organization.")},
            )
        allowed = [value for value in (self.allowed_file_types or []) if value]
        if not allowed:
            raise ValidationError(
                {
                    "allowed_file_types": _(
                        "Select at least one submission file type.",
                    ),
                },
            )
        normalized: list[str] = []
        for value in allowed:
            if value not in SubmissionFileType.values:
                raise ValidationError(
                    {
                        "allowed_file_types": _(
                            "'%(value)s' is not a supported submission file type.",
                        )
                        % {"value": value},
                    },
                )
            if value not in normalized:
                normalized.append(value)
        self.allowed_file_types = normalized

    def save(self, *args, **kwargs):
        self.full_clean()
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def can_view(self, *, user: User) -> bool:
        """
        Check if the given user can view this workflow.

        Access is granted if either:
        - User has WORKFLOW_VIEW permission in the workflow's org (org member), OR
        - User has an active WorkflowAccessGrant for this workflow (guest)
        """
        if not user or not user.is_authenticated:
            return False

        # Org member check (existing behavior)
        if user.has_perm(PermissionCode.WORKFLOW_VIEW.value, self):
            return True

        # Guest grant check
        return self.access_grants.filter(user=user, is_active=True).exists()

    def can_delete(self, *, user: User) -> bool:
        """
        Check if the given user can delete this workflow.
        Requires the ``workflow_edit`` permission in the workflow's org.
        """
        if not user or not user.is_authenticated:
            return False

        return user.has_perm(PermissionCode.WORKFLOW_EDIT.value, self)

    def can_execute(self, *, user: User) -> bool:
        """
        Check if the given user can execute (run) this workflow.

        Access is granted if either:
        - User has WORKFLOW_LAUNCH permission in the workflow's org (org member), OR
        - User has an active WorkflowAccessGrant for this workflow (guest)

        The workflow must also be active for execution to be allowed.
        """
        if not self.is_active:
            return False
        if not user or not user.is_authenticated:
            return False

        # Org member check (existing behavior)
        if user.has_perm(PermissionCode.WORKFLOW_LAUNCH.value, self):
            return True

        # Guest grant check
        return self.access_grants.filter(user=user, is_active=True).exists()

    def can_edit(self, *, user: User) -> bool:
        """
        Check if the given user can edit this workflow.
        Requires the ``workflow_edit`` permission in the workflow's organization.
        """
        if not user or not user.is_authenticated:
            return False

        return user.has_perm(PermissionCode.WORKFLOW_EDIT.value, self)

    def allowed_file_type_labels(self) -> list[str]:
        labels: list[str] = []
        for value in self.allowed_file_types or []:
            try:
                labels.append(str(SubmissionFileType(value).label))
            except Exception:
                labels.append(str(value))
        return labels

    def supports_file_type(self, file_type: str) -> bool:
        normalized = (file_type or "").lower()
        return normalized in {ft.lower() for ft in (self.allowed_file_types or [])}

    def validator_is_compatible(self, validator) -> bool:
        if not validator:
            return True
        validator_types = set(
            getattr(validator, "supported_file_types", []) or [],
        )
        workflow_types = set(self.allowed_file_types or [])
        return bool(
            {ft.lower() for ft in workflow_types}
            & {ft.lower() for ft in validator_types}
        )

    def first_incompatible_step(self, file_type: str):
        if not file_type:
            return None
        normalized = file_type.lower()
        steps = self.steps.select_related("validator").all()
        for step in steps:
            validator = step.validator
            if validator and hasattr(validator, "supports_file_type"):
                if not validator.supports_file_type(normalized):
                    return step
        return None

    @transaction.atomic
    def clone_to_new_version(self, user) -> Workflow:
        """
        Create an identical workflow with version+1 and copied steps.
        Locks old version.
        """
        sibling_versions = list(
            Workflow.objects.filter(org=self.org, slug=self.slug)
            .exclude(pk=self.pk)
            .values_list("version", flat=True),
        )
        sibling_versions.append(self.version)

        next_version = self._determine_next_version_label(sibling_versions)

        new = Workflow.objects.create(
            org=self.org,
            user=user,
            name=self.name,
            slug=self.slug,
            version=next_version,
            is_locked=False,
            is_active=self.is_active,
            allowed_file_types=list(self.allowed_file_types or []),
            data_retention=self.data_retention,
        )
        steps = []
        for step in self.steps.all().order_by("order"):
            step.pk = None
            step.workflow = new
            steps.append(step)
        WorkflowStep.objects.bulk_create(steps)
        self.is_locked = True
        self.save(update_fields=["is_locked"])
        return new

    def _determine_next_version_label(self, versions) -> str:
        """Return a simple, unique version label for the cloned workflow."""

        numeric_versions: list[Decimal] = []
        for raw in versions:
            if raw is None:
                continue
            candidate = str(raw).strip()
            if not candidate:
                continue
            try:
                numeric_versions.append(Decimal(candidate))
            except InvalidOperation:
                continue

        if numeric_versions:
            highest = max(numeric_versions)
            new_version = highest + Decimal(1)
            if new_version == new_version.to_integral():
                return str(int(new_version))
            return format(new_version.normalize(), "f")

        # Fall back to a simple incremental label.
        return "1"

    @property
    def is_advanced(self) -> bool:
        """
        Check if this workflow uses any advanced (high-compute) validators.

        Advanced workflows consume credits instead of basic launch quota.
        This is used by billing enforcement to determine which meter to use.

        Returns:
            True if any step uses an advanced validator type.
        """
        from validibot.validations.constants import ADVANCED_VALIDATION_TYPES

        return self.steps.filter(
            validator__validation_type__in=ADVANCED_VALIDATION_TYPES,
        ).exists()

    @property
    def get_public_info(self) -> WorkflowPublicInfo:
        public_info, _ = WorkflowPublicInfo.objects.get_or_create(workflow=self)
        return public_info


class WorkflowPublicInfo(TimeStampedModel):
    workflow = models.OneToOneField(
        Workflow,
        on_delete=models.CASCADE,
        related_name="public_info",
    )
    title = models.CharField(
        max_length=200,
        default="",
        help_text=_(
            "Optional title to show on the public info page. "
            "If blank, the Workflow name will be used.",
        ),
    )
    content_md = models.TextField()  # user-authored Markdown
    content_html = models.TextField(editable=False)  # cached sanitized HTML

    show_steps = models.BooleanField(
        default=True,
        help_text=_("Whether to show the workflow steps on the public info page."),
    )

    def __str__(self):
        return f"Public info for {self.workflow}"

    def save(self, *args, **kwargs):
        self.compile_content()
        super().save(*args, **kwargs)

    def compile_content(self):
        try:
            self.content_html = render_markdown_safe(self.content_md)
        except Exception:
            logger.exception("Error rendering markdown for workflow public info")
            self.content_html = ""

    def get_title(self) -> str:
        if self.title and self.title.strip():
            return self.title.strip()
        return self.workflow.name

    def get_html_content(self) -> str:
        return self.content_html or ""


class WorkflowStep(TimeStampedModel):
    """
    Ordered unit of work within a workflow.

    Each step is either a validator execution or an action (never both). Validator
    steps may optionally link a `Ruleset` to override the validator's default
    assertions; action steps skip rulesets and instead reference a concrete
    `Action` subclass (Slack message, signed certificate, etc.) that performs a
    side effect. `config` stores per-step JSON tweaks consumed by the validator or
    action at runtime such as severity thresholds or templated text.
    """

    class Meta:
        unique_together = [
            (
                "workflow",
                "order",
            ),
        ]
        ordering = ["order"]
        constraints = [
            models.CheckConstraint(
                name="workflowstep_validator_xor_action",
                condition=(
                    Q(validator__isnull=False, action__isnull=True)
                    | Q(validator__isnull=True, action__isnull=False)
                ),
            ),
        ]

    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name="steps",
    )

    order = models.PositiveIntegerField()  # 10,20,30... leave gaps for inserts

    name = models.CharField(
        max_length=200,
        blank=True,
        default="",
    )
    description = models.CharField(
        max_length=2000,
        blank=True,
        default="",
        help_text=_("Brief description to help users understand what this step does."),
    )
    notes = models.CharField(
        max_length=2000,
        blank=True,
        default="",
        help_text=_(
            "Author notes about this step (visible only by you and other users "
            "with author permissions for this workflow).",
        ),
    )
    display_schema = models.BooleanField(
        default=False,
        help_text=_("Allow launchers to view this schema in public workflow pages."),
    )

    validator = models.ForeignKey(
        "validations.Validator",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )

    action = models.ForeignKey(
        Action,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="workflow_steps",
    )

    ruleset = models.ForeignKey(
        "validations.Ruleset",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    # Optional per-step config (e.g., severity thresholds, mapping)
    config = models.JSONField(default=dict, blank=True)

    @property
    def step_number(self) -> int:
        """Return the display position for this step based on its order."""
        if not self.order:
            return 1
        return max(1, math.ceil(self.order / 10))

    @property
    def step_number_display(self) -> str:
        """Return a localized display string for this step's number."""
        step_number = self.step_number
        return _("Step") + f" {step_number}"

    def clean(self):
        super().clean()

        if (
            WorkflowStep.objects.filter(workflow=self.workflow, order=self.order)
            .exclude(pk=self.pk)
            .exists()
        ):
            raise ValidationError({"order": _("Order already used in this workflow.")})

        # Ensure the ruleset chosen matches the validator's type
        if bool(self.validator_id) == bool(self.action_id):
            raise ValidationError(
                {
                    "validator": _(
                        "Specify either a validator or an action for this step.",
                    ),
                    "action": _(
                        "Specify either a validator or an action for this step.",
                    ),
                },
            )

        if (
            self.validator
            and self.ruleset
            and (self.ruleset.ruleset_type != self.validator.validation_type)
        ):
            raise ValidationError(
                {
                    "ruleset": _("Ruleset type must match validator type."),
                },
            )

        if self.action and self.display_schema:
            self.display_schema = False


class WorkflowRoleAccess(models.Model):
    """
    Grants access to a workflow to users holding specific roles in the workflow's org.
    Example: allow all 'ADMIN' or 'OWNER' members of the org.
    """

    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name="role_access",
    )

    role = models.ForeignKey(
        Role,
        on_delete=models.CASCADE,
        related_name="workflow_role_access",
    )

    class Meta:
        unique_together = [("workflow", "role")]
        indexes = [models.Index(fields=["workflow", "role"])]

    def __str__(self):
        return f"{self.workflow_id}:{self.role}"


class WorkflowAccessGrant(TimeStampedModel):
    """
    Grants a user (typically external) access to a specific workflow without
    requiring org membership. Used for cross-organization workflow sharing.

    Workflow Guests are users who have access grants but no org membership.
    Their usage is billed/metered against the workflow owner's org.

    This is distinct from WorkflowRoleAccess which grants access based on
    org membership roles. WorkflowAccessGrant is for external users who
    are not members of the workflow's organization.
    """

    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name="access_grants",
        help_text=_("The workflow this grant provides access to."),
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="workflow_grants",
        help_text=_("The user who has been granted access."),
    )
    granted_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="granted_workflow_access",
        help_text=_("The user who created this grant."),
    )
    is_active = models.BooleanField(
        default=True,
        help_text=_("Whether this grant is currently active."),
    )
    notes = models.TextField(
        blank=True,
        default="",
        help_text=_("Optional notes about this access grant."),
    )

    class Meta:
        unique_together = [("workflow", "user")]
        indexes = [
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["workflow", "is_active"]),
        ]
        verbose_name = _("workflow access grant")
        verbose_name_plural = _("workflow access grants")

    def __str__(self):
        return f"{self.user} -> {self.workflow.name}"

    def revoke(self) -> None:
        """Revoke this access grant."""
        self.is_active = False
        self.save(update_fields=["is_active", "modified"])


class WorkflowInvite(TimeStampedModel):
    """
    Invitation for an external user to access a specific workflow as a guest.

    Unlike PendingInvite (for org membership), accepting this invite creates a
    WorkflowAccessGrant but NOT a Membership. The invited user operates as a
    Workflow Guest without an organization context.

    Workflow invites enable cross-org sharing where:
    - The inviter is an author in the workflow's org
    - The invitee may or may not have an existing account
    - Upon acceptance, the invitee gets access to the specific workflow only
    - Usage is billed to the workflow owner's org
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending")
        ACCEPTED = "ACCEPTED", _("Accepted")
        DECLINED = "DECLINED", _("Declined")
        CANCELED = "CANCELED", _("Canceled")
        EXPIRED = "EXPIRED", _("Expired")

    # Default invite expiry: 7 days
    DEFAULT_EXPIRY_DAYS = 7

    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    workflow = models.ForeignKey(
        Workflow,
        on_delete=models.CASCADE,
        related_name="invites",
        help_text=_("The workflow this invite grants access to."),
    )
    inviter = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="sent_workflow_invites",
        help_text=_("The user who sent this invite."),
    )
    invitee_user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="received_workflow_invites",
        null=True,
        blank=True,
        help_text=_("The invited user, if they already have an account."),
    )
    invitee_email = models.EmailField(
        blank=True,
        help_text=_("Email address of invitee (used when inviting non-users)."),
    )
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    token = models.UUIDField(
        default=uuid.uuid4,
        editable=False,
        unique=True,
        help_text=_("Unique token for invite acceptance URL."),
    )
    expires_at = models.DateTimeField(
        help_text=_("When this invite expires."),
    )

    class Meta:
        ordering = ["-created"]
        verbose_name = _("workflow invite")
        verbose_name_plural = _("workflow invites")
        indexes = [
            models.Index(fields=["token"]),
            models.Index(fields=["invitee_email", "status"]),
            models.Index(fields=["workflow", "status"]),
        ]

    def __str__(self):
        target = self.invitee_user or self.invitee_email
        return f"Invite to {self.workflow.name} for {target}"

    @classmethod
    def create_with_expiry(
        cls,
        *,
        workflow: Workflow,
        inviter: User,
        invitee_email: str,
        invitee_user: User | None = None,
        expiry_days: int | None = None,
        send_email: bool = True,
    ) -> WorkflowInvite:
        """
        Create a new workflow invite with default expiry.

        Args:
            workflow: The workflow to grant access to.
            inviter: The user sending the invite.
            invitee_email: Email of the person being invited.
            invitee_user: Optional existing user if email matches.
            expiry_days: Days until expiry (default: 7).
            send_email: Whether to send an invitation email (default: True).

        Returns:
            The created WorkflowInvite instance.
        """
        from datetime import timedelta

        from django.utils import timezone

        days = expiry_days or cls.DEFAULT_EXPIRY_DAYS
        expires_at = timezone.now() + timedelta(days=days)

        invite = cls.objects.create(
            workflow=workflow,
            inviter=inviter,
            invitee_email=invitee_email,
            invitee_user=invitee_user,
            expires_at=expires_at,
        )

        if send_email:
            from validibot.workflows.emails import send_workflow_invite_email

            send_workflow_invite_email(invite)

        return invite

    def mark_expired_if_needed(self) -> bool:
        """
        Check if invite has expired and update status if so.

        Returns:
            True if the invite was marked as expired, False otherwise.
        """
        from django.utils import timezone

        if self.status != self.Status.PENDING:
            return False

        if timezone.now() >= self.expires_at:
            self.status = self.Status.EXPIRED
            self.save(update_fields=["status", "modified"])
            return True

        return False

    def accept(self, user: User | None = None) -> WorkflowAccessGrant:
        """
        Accept this invite and create a WorkflowAccessGrant.

        Args:
            user: The user accepting the invite. If not provided, uses
                  invitee_user. Required if invitee_user is not set.

        Returns:
            The created WorkflowAccessGrant.

        Raises:
            ValueError: If invite is not in PENDING status or no user provided.
        """
        if self.status != self.Status.PENDING:
            msg = f"Cannot accept invite with status {self.status}"
            raise ValueError(msg)

        accepting_user = user or self.invitee_user
        if not accepting_user:
            msg = "No user provided to accept invite"
            raise ValueError(msg)

        # Check for expiry first
        if self.mark_expired_if_needed():
            msg = "Invite has expired"
            raise ValueError(msg)

        # Create the access grant
        grant, _created = WorkflowAccessGrant.objects.get_or_create(
            workflow=self.workflow,
            user=accepting_user,
            defaults={
                "granted_by": self.inviter,
                "is_active": True,
            },
        )

        # If grant already existed but was inactive, reactivate it
        if not _created and not grant.is_active:
            grant.is_active = True
            grant.granted_by = self.inviter
            grant.save(update_fields=["is_active", "granted_by", "modified"])

        # Update invite status
        self.status = self.Status.ACCEPTED
        if not self.invitee_user:
            self.invitee_user = accepting_user
        self.save(update_fields=["status", "invitee_user", "modified"])

        return grant

    def decline(self) -> None:
        """Decline this invite."""
        if self.status != self.Status.PENDING:
            msg = f"Cannot decline invite with status {self.status}"
            raise ValueError(msg)

        self.status = self.Status.DECLINED
        self.save(update_fields=["status", "modified"])

    def cancel(self) -> None:
        """Cancel this invite (called by the inviter)."""
        if self.status != self.Status.PENDING:
            msg = f"Cannot cancel invite with status {self.status}"
            raise ValueError(msg)

        self.status = self.Status.CANCELED
        self.save(update_fields=["status", "modified"])

    @property
    def is_expired(self) -> bool:
        """Check if invite has expired without updating status."""
        from django.utils import timezone

        return self.status == self.Status.EXPIRED or timezone.now() >= self.expires_at

    @property
    def is_pending(self) -> bool:
        """Check if invite is still pending and not expired."""
        return self.status == self.Status.PENDING and not self.is_expired
