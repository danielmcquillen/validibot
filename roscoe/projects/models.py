from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from roscoe.users.models import Organization


# Create your models here.
class Project(TimeStampedModel):
    """
    Optional namespace under an org. Helps teams separate keys/usage.
    """

    class Meta:
        unique_together = [("org", "slug")]
        indexes = [models.Index(fields=["org", "slug"])]

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

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.org.name} - {self.name}"
