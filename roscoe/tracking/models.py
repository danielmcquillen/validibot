from django.db import models
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from roscoe.projects.models import Project
from roscoe.tracking.constants import TrackingEventType
from roscoe.users.models import Organization, User


# BaseEVent is an abstract model that can be extended by other event models.
class BaseEvent(TimeStampedModel):
    """
    Model to track events related to projects.
    """

    class Meta:
        abstract = True
        indexes = [
            models.Index(
                fields=[
                    "project",
                    "event_type",
                ]
            )
        ]

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="tracking_events",
        help_text=_("The project this event is associated with."),
    )

    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="tracking_events",
        help_text=_("The organization this event is associated with."),
    )

    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tracking_events",
        help_text=_("The user who triggered this event."),
    )

    extra_data = models.JSONField(
        blank=True,
        null=True,
        help_text=_("Optional additional data related to the event."),
    )

    def __str__(self):
        return f"{self.project.name} - {self.event_type}"


class TrackingEvent(BaseEvent):
    """
    Model to track basic interaction events within a project.
    """

    class Meta:
        indexes = [
            models.Index(fields=["project", "event_type"]),
        ]

    event_type = models.CharField(
        max_length=100,
        choices=TrackingEventType.choices,
        blank=False,
        null=False,
    )

    def __str__(self):
        return f"{self.project.name} - {self.event_type}"
