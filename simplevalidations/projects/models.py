from __future__ import annotations

import secrets

from django.core.validators import RegexValidator
from django.db import models
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from model_utils.models import TimeStampedModel

from simplevalidations.users.models import Organization

LUMINANCE_THRESHOLD = 150
COLOR_LENGTH = 7


# Create your models here.
class ProjectQuerySet(models.QuerySet):
    def active(self):
        return self.filter(is_active=True)

    def soft_delete(self):
        for project in self:
            project.soft_delete()


class ProjectManager(models.Manager.from_queryset(ProjectQuerySet)):
    def get_queryset(self):
        return super().get_queryset().filter(is_active=True)


class ProjectAllManager(models.Manager.from_queryset(ProjectQuerySet)):
    pass


def generate_random_color() -> str:
    """
    Return a random hex colour in the format #RRGGBB.
    """
    return f"#{secrets.token_hex(3).upper()}"


class Project(TimeStampedModel):
    """
    Optional namespace under an org. Helps teams separate keys/usage.
    """

    class Meta:
        unique_together = [("org", "slug")]
        indexes = [
            models.Index(fields=["org", "slug"]),
            models.Index(fields=["org", "is_active"]),
            models.Index(fields=["is_active", "deleted_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["org"],
                condition=models.Q(is_default=True),
                name="uq_project_org_single_default",
            ),
        ]

    objects = ProjectManager()
    all_objects = ProjectAllManager()

    DEFAULT_BADGE_COLOR = "#6C757D"
    HEX_COLOR_MESSAGE = _("Use a hex color code like #1F883D.")

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="projects",
        help_text=_("The organization this project belongs to."),
    )

    name = models.CharField(
        max_length=200,
        blank=False,
        null=False,
        help_text=_("Name of the project, e.g. 'My Project'"),
    )

    description = models.TextField(
        blank=True,
        default="",
        help_text=_("Optional longer description of the project."),
    )

    slug = models.SlugField(
        blank=True,
        null=False,
        help_text=_("A unique identifier for the project, used in URLs."),
    )

    is_default = models.BooleanField(
        default=False,
        help_text=_("Indicates the default project for an organization."),
    )

    is_active = models.BooleanField(
        default=True,
        help_text=_("Soft-delete flag."),
    )

    deleted_at = models.DateTimeField(null=True, blank=True)
    color = models.CharField(
        max_length=7,
        default=generate_random_color,
        validators=[
            RegexValidator(
                regex=r"^#[0-9A-Fa-f]{6}$",
                message=HEX_COLOR_MESSAGE,
            ),
        ],
        help_text=_("Hex color used when displaying project badges (e.g. #1F883D)."),
    )

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        if not self.color:
            self.color = generate_random_color()
        if self.color:
            self.color = self.color.upper()
        super().save(*args, **kwargs)

    def can_delete(self) -> bool:
        return not self.is_default

    def soft_delete(self) -> None:
        if not self.can_delete():
            raise ValueError(_("Default projects cannot be deleted."))
        if not self.is_active:
            return
        from simplevalidations.integrations.models import OutboundEvent  # noqa: PLC0415
        from simplevalidations.submissions.models import Submission  # noqa: PLC0415
        from simplevalidations.tracking.models import TrackingEvent  # noqa: PLC0415
        from simplevalidations.validations.models import ValidationRun  # noqa: PLC0415
        from simplevalidations.workflows.models import Workflow  # noqa: PLC0415

        now = timezone.now()
        Submission.objects.filter(project=self).update(project=None)
        ValidationRun.objects.filter(project=self).update(project=None)
        TrackingEvent.objects.filter(project=self).update(project=None)
        OutboundEvent.objects.filter(project=self).update(project=None)
        Workflow.objects.filter(project=self).update(project=None)

        self.is_active = False
        self.deleted_at = now
        self.save(update_fields=["is_active", "deleted_at"])

    def __str__(self):
        return f"{self.org.name} - {self.name}"

    def _parsed_rgb(self) -> tuple[int, int, int] | None:
        color = (self.color or "").strip()
        if not color or len(color) != COLOR_LENGTH or not color.startswith("#"):
            return None
        hex_value = color[1:]
        try:
            r = int(hex_value[0:2], 16)
            g = int(hex_value[2:4], 16)
            b = int(hex_value[4:6], 16)
        except ValueError:
            return None
        return r, g, b

    @property
    def badge_text_color(self) -> str:
        """
        Return a contrasting text color for badges that use the project color.
        """
        components = self._parsed_rgb()
        if not components:
            return "#1F2328"
        r, g, b = components
        # Calculate relative luminance using standard coefficients.
        luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
        return "#1F2328" if luminance > LUMINANCE_THRESHOLD else "#FFFFFF"

    @property
    def badge_border_color(self) -> str:
        """
        Return a lighter border color for badges to mimic GitHub-style tags.
        """
        components = self._parsed_rgb()
        if not components:
            return self.DEFAULT_BADGE_COLOR
        r, g, b = components
        lighten = (
            min(255, int(r + (255 - r) * 0.25)),
            min(255, int(g + (255 - g) * 0.25)),
            min(255, int(b + (255 - b) * 0.25)),
        )
        return "#{:02X}{:02X}{:02X}".format(*lighten)
