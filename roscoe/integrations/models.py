from django.contrib.postgres.fields import ArrayField
from django.db import models

# Create your models here.
# roscoe/integrations/models.py
from django_extensions.db.models import TimeStampedModel

from roscoe.events.constants import EventType
from roscoe.users.models import Organization


class WebhookEndpoint(TimeStampedModel):
    """
    Represents a webhook endpoint for an organization.

    This model is used to store the URL and secret for the webhook,
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
    Represents a delivery of a webhook event to an endpoint.
    This model is used to track the status of webhook deliveries,
    including the event type, payload, status code, and success flag.
    """

    endpoint = models.ForeignKey(
        WebhookEndpoint,
        on_delete=models.CASCADE,
        related_name="deliveries",
    )

    event_type = models.CharField(max_length=64)

    payload = models.JSONField()

    status_code = models.IntegerField(null=True, blank=True)

    success = models.BooleanField(default=False)

    attempt = models.IntegerField(default=1)

    error = models.TextField(
        blank=True,
        default="",
    )
