from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db import transaction
from django.db.models import Exists
from django.db.models import OuterRef
from django.db.models import Q
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from roscoe.users.constants import RoleCode
from roscoe.users.models import MembershipRole
from roscoe.users.models import Organization
from roscoe.users.models import Role
from roscoe.users.models import User


class WorkflowQuerySet(models.QuerySet):
    """
    A custom queryset for Workflow model to add user-specific filtering methods.
    This lets us easily get workflows a user has access to based on their organization
    """

    def for_user(
        self,
        user: User,
        required_role_code: RoleCode | None = None,
    ) -> WorkflowQuerySet:
        """
        Get workflows accessible to the given user. If required_role is provided,
        only return workflows where the user has that role in the workflow's
        organization.
        """

        if not user:
            err_msg = "User must be provided"
            raise ValueError(err_msg)

        # Workflows in any org the user belongs to
        user_org_ids = user.orgs.values_list("id", flat=True)
        qs = self.filter(org_id__in=user_org_ids)

        # Fast path: if no role required, return all workflows in user's orgs
        if not required_role_code:
            return qs

        # Exact-role requirement: user must hold required_role_code in that org
        has_required_role = MembershipRole.objects.filter(
            membership__user=user,
            membership__organization_id=OuterRef("org_id"),
            membership__is_active=True,
            role__code=required_role_code,
        )

        return qs.filter(Exists(has_required_role))


class WorkflowManager(models.Manager):
    def get_queryset(self):
        return WorkflowQuerySet(self.model, using=self._db)

    def for_user(self, user: User, required_role_code: RoleCode | None = None):
        return self.get_queryset().for_user(user, required_role_code=required_role_code)


class Workflow(TimeStampedModel):
    """
    Reusable, versioned definition of a sequence of validation steps.
    """

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
        help_text=_("A unique identifier for the workflow, used in URLs."),
    )

    version = models.PositiveIntegerField()

    is_locked = models.BooleanField(
        default=False,
    )

    # Methods
    # ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

    def clean(self):
        if not self.name or not self.name.strip():
            raise ValidationError({"name": _("Name is required.")})

    def save(self, *args, **kwargs):
        self.full_clean()
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def can_execute(self, *, user: User) -> bool:
        """
        Check if the given user can execute this workflow.
        Requires that the user has the EXECUTOR role in the workflow's org.
        """
        if not user or not user.is_authenticated:
            return False

        can_execute = (
            Workflow.objects.for_user(
                user,
                required_role_code=RoleCode.EXECUTE,
            )
            .filter(pk=self.pk)
            .exists()
        )

        return can_execute

    @transaction.atomic
    def clone_to_new_version(self, user) -> Workflow:
        """
        Create an identical workflow with version+1 and copied steps.
        Locks old version.
        """
        latest_version = (
            Workflow.objects.filter(org=self.org, slug=self.slug)
            .exclude(pk=self.pk)
            .aggregate(models.Max("version"))["version__max"]
            or self.version
        )
        new = Workflow.objects.create(
            org=self.org,
            user=user,
            name=self.name,
            slug=self.slug,
            version=latest_version + 1,
            is_locked=False,
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


class WorkflowStep(TimeStampedModel):
    """
    One step in a workflow, ordered. Linear for MVP.
    """

    class Meta:
        unique_together = [
            (
                "workflow",
                "order",
            ),
        ]
        ordering = ["order"]

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

    validator = models.ForeignKey(
        "validations.Validator",
        on_delete=models.PROTECT,
    )

    ruleset = models.ForeignKey(
        "validations.Ruleset",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )

    # Optional per-step config (e.g., severity thresholds, mapping)
    config = models.JSONField(default=dict, blank=True)

    def clean(self):
        super().clean()

        if (
            WorkflowStep.objects.filter(workflow=self.workflow, order=self.order)
            .exclude(pk=self.pk)
            .exists()
        ):
            raise ValidationError({"order": _("Order already used in this workflow.")})

        if self.ruleset and self.ruleset.type != self.validator.type:
            raise ValidationError(
                {
                    "ruleset": _("Ruleset type must match validator type."),
                },
            )


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
