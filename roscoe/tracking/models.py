from django.db import models
from django.utils.translation import gettext_lazy as _
from django_extensions.db.models import TimeStampedModel

from roscoe.events.constants import AppEventType
from roscoe.projects.models import Project
from roscoe.tracking.constants import TrackingEventType
from roscoe.users.models import Organization
from roscoe.users.models import User


class BaseEvent(TimeStampedModel):
    """
    Model to track events related to projects.
    This is an abstract model that can be extended by other event models.
    """

    class Meta:
        abstract = True
        indexes = [
            models.Index(
                fields=[
                    "project",
                    "event_type",
                ],
            ),
        ]

    event_type = models.CharField(
        max_length=100,
        blank=False,
        null=False,
    )

    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="tracking_events",
        help_text=_("The project this event is associated with."),
        null=True,
        blank=True,
    )

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="tracking_events",
        help_text=_("The organization this event is associated with."),
        null=True,
        blank=True,
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

    app_event_type = models.CharField(
        max_length=100,
        choices=AppEventType.choices,
        default="",
        help_text=_("Specific application event identifier when type is APP_EVENT."),
    )

    def __str__(self):
        base = self.event_type
        if self.event_type == TrackingEventType.APP_EVENT and self.app_event_type:
            base = f"{base}:{self.app_event_type}"
        project_name = getattr(self.project, "name", "") or "( no project )"
        return f"{project_name} - {base}"
