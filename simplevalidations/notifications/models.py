from __future__ import annotations

from uuid import uuid4

from django.db import models
from django.utils.translation import gettext_lazy as _

from simplevalidations.users.models import Organization, User, PendingInvite


class Notification(models.Model):
    """
    Generic notification record tied to a user and organization.
    Invite notifications optionally link to a PendingInvite for integrity.
    """

    class Type(models.TextChoices):
        INVITE = "invite", _("Invite")
        SYSTEM_ALERT = "system_alert", _("System alert")

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    type = models.CharField(max_length=32, choices=Type.choices)
    payload = models.JSONField(default=dict)
    invite = models.ForeignKey(
        PendingInvite,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Notification {self.type} for {self.user}"

    @property
    def is_unread(self) -> bool:
        return self.read_at is None
