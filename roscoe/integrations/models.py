from __future__ import annotations

from django.contrib.postgres.fields import ArrayField
from django.db import models
from django_extensions.db.models import TimeStampedModel

from roscoe.events.constants import EventType
from roscoe.projects.models import Project
from roscoe.users.models import Organization


class WebhookEndpoint(TimeStampedModel):
    """
    Represents a webhook endpoint for an organization.

    This model is used to store the external URL and secret for the webhook,
    along with the event types that the endpoint is interested in.
    """

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="webhooks",
    )

    url = models.URLField()

    secret = models.CharField(max_length=128, blank=True, default="")

    is_active = models.BooleanField(default=True)

    event_types = ArrayField(
        base_field=models.CharField(
            max_length=32,
            choices=EventType.choices,
        ),
        default=list,
        blank=True,
        help_text="Subset of official event types to deliver.",
    )

    def clean(self):
        super().clean()
        # Deduplicate and keep stable ordering
        values = list(dict.fromkeys(self.event_types or []))
        self.event_types = values


class WebhookDelivery(TimeStampedModel):
    """
    Represents one logical delivery of a webhook event to a
    specific endpoint for a specific event.
    """

    class Meta:
        unique_together = [
            (
                "endpoint",
                "event",
            )
        ]
        indexes = [
            models.Index(fields=["endpoint", "created"]),
            models.Index(fields=["success", "created"]),
            models.Index(fields=["next_retry_at"]),
        ]

    endpoint = models.ForeignKey(
        WebhookEndpoint,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )

    event = models.ForeignKey(
        "OutboundEvent",
        on_delete=models.CASCADE,
        related_name="deliveries",
    )

    attempt = models.PositiveIntegerField(default=0)

    status_code = models.IntegerField(null=True, blank=True)

    success = models.BooleanField(default=False)

    error = models.TextField(blank=True, default="")

    last_attempt_at = models.DateTimeField(null=True, blank=True)

    next_retry_at = models.DateTimeField(
        null=True, blank=True
    )  # backoff scheduler hook


class OutboundEvent(TimeStampedModel):
    """
    Normalized event to fan-out to webhooks.
    """

    class Meta:
        indexes = [
            models.Index(
                fields=[
                    "org",
                    "event_type",
                    "created",
                ]
            ),
            models.Index(
                fields=[
                    "resource_type",
                    "resource_id",
                ]
            ),
        ]

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="outbound_events",
    )

    project = models.ForeignKey(
        Project,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    event_type = models.CharField(max_length=64)  # constrained elsewhere via choices

    resource_type = models.CharField(max_length=64)  # e.g. "validation_run"

    resource_id = models.CharField(max_length=64)

    payload = models.JSONField()

    # optional dedupe key if you ever re-emit
    dedupe_key = models.CharField(max_length=128, blank=True, default="")
