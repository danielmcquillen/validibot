from __future__ import annotations

from uuid import uuid4

from attr import has
from django.db import models
from django.utils.translation import gettext_lazy as _
from regex import P

from simplevalidations.users.models import Organization, PendingInvite, User


class Notification(models.Model):
    """
    Generic notification record tied to a user and organization.
    Invite notifications optionally link to a PendingInvite for integrity.
    """

    class Type(models.TextChoices):
        INVITE = "invite", _("Invite")
        SYSTEM_ALERT = "system_alert", _("System alert")

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="notifications"
    )
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
    dismissed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Notification {self.type} for {self.user}"

    @property
    def is_unread(self) -> bool:
        return self.read_at is None

    @property
    def is_dismissed(self) -> bool:
        return self.dismissed_at is not None

    @property
    def can_dismiss(self) -> bool:
        """
        Invite handling:
        - Invitee cannot dismiss while invite is pending.
        - Inviter can always dismiss.
        - Once invite is no longer pending (accepted/declined/expired/canceled),
          any recipient can dismiss.
        - Non-invite notifications are always dismissible.
        """
        if not self.invite:
            return True
        if self.user != self.invite.invitee_user:
            return True
        # An invitee can dismiss only if the invite is no longer pending.
        return self.invite.status != PendingInvite.Status.PENDING
