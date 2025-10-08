from __future__ import annotations

from django.db import models
from django.utils import timezone
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from simplevalidations.users.models import Organization


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

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def can_delete(self) -> bool:
        return not self.is_default

    def soft_delete(self) -> None:
        if not self.can_delete():
            raise ValueError("Default projects cannot be deleted.")
        if not self.is_active:
            return
        from simplevalidations.submissions.models import Submission
        from simplevalidations.validations.models import ValidationRun
        from simplevalidations.tracking.models import TrackingEvent
        from simplevalidations.integrations.models import OutboundEvent

        now = timezone.now()
        Submission.objects.filter(project=self).update(project=None)
        ValidationRun.objects.filter(project=self).update(project=None)
        TrackingEvent.objects.filter(project=self).update(project=None)
        OutboundEvent.objects.filter(project=self).update(project=None)

        self.is_active = False
        self.deleted_at = now
        self.save(update_fields=["is_active", "deleted_at"])

    def __str__(self):
        return f"{self.org.name} - {self.name}"
