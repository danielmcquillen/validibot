from __future__ import annotations

import logging
import math
import uuid
from decimal import Decimal
from decimal import InvalidOperation
from typing import TYPE_CHECKING

from django.contrib.postgres.fields import ArrayField
from django.core.exceptions import ValidationError
from django.db import models
from django.db import transaction
from django.db.models import Exists
from django.db.models import OuterRef
from django.db.models import Q
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from simplevalidations.actions.models import Action
from simplevalidations.core.mixins import FeaturedImageMixin
from simplevalidations.core.utils import render_markdown_safe
from simplevalidations.projects.models import Project
from simplevalidations.submissions.constants import SubmissionFileType
from simplevalidations.users.models import Membership
from simplevalidations.users.models import Organization
from simplevalidations.users.models import Role
from simplevalidations.users.models import User
from simplevalidations.users.permissions import PermissionCode
from simplevalidations.users.permissions import roles_for_permission

if TYPE_CHECKING:
    from simplevalidations.users.constants import RoleCode

logger = logging.getLogger(__name__)


class WorkflowQuerySet(models.QuerySet):
    """
    A custom queryset for Workflow model to add user-specific filtering methods.
    This lets us easily get workflows a user has access to based on their membership
    to organizations.
    """

    def for_user(
        self,
        user: User,
        required_role_code: RoleCode | None = None,
    ) -> WorkflowQuerySet:
        """
        Get workflows accessible to the given user.

        If required_role is provided,
        only return workflows where the user has that role in the workflow's
        organization.

        Otherwise, return all workflows where the user is an active member of the
        workflow's organization. Note this doesn't mean they can execute the workflow;
        that requires the EXECUTOR role specifically.

        """

        if not getattr(user, "is_authenticated", False):
            return self.none()

        allowed_view_roles = roles_for_permission(PermissionCode.WORKFLOW_VIEW)
        subq = Membership.objects.filter(
            org=OuterRef("org_id"),
            user=user,
            is_active=True,
        )
        if required_role_code:
            subq = subq.filter(roles__code=required_role_code)
        else:
            subq = subq.filter(roles__code__in=allowed_view_roles)

        return (
            self.annotate(
                _has_access=Exists(subq) | Q(user_id=user.id),
            )
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
        Requires the ``workflow_view`` permission in the workflow's org.
        """
        if not user or not user.is_authenticated:
            return False

        return user.has_perm(PermissionCode.WORKFLOW_VIEW.value, self)

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
        Check if the given user can execute this workflow.
        Requires the ``workflow_launch`` permission in the workflow's org.
        """
        if not self.is_active:
            return False
        if not user or not user.is_authenticated:
            return False

        return user.has_perm(PermissionCode.WORKFLOW_LAUNCH.value, self)

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
