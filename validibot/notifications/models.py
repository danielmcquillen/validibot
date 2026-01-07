from __future__ import annotations

from uuid import uuid4

from django.db import models
from django.utils.translation import gettext_lazy as _

from validibot.core.constants import InviteStatus
from validibot.users.models import MemberInvite
from validibot.users.models import Organization
from validibot.users.models import User


class Notification(models.Model):
    """
    Generic notification record tied to a user and organization.

    Invite notifications link to the appropriate invite model:
    - member_invite: MemberInvite (org membership invites)
    - guest_invite: GuestInvite (org-level guest access invites)
    - workflow_invite: WorkflowInvite (per-workflow guest invites)
    """

    class Type(models.TextChoices):
        MEMBER_INVITE = "member_invite", _("Member invite")
        GUEST_INVITE = "guest_invite", _("Guest invite")
        WORKFLOW_INVITE = "workflow_invite", _("Workflow invite")
        SYSTEM_ALERT = "system_alert", _("System alert")

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    type = models.CharField(max_length=32, choices=Type.choices)
    payload = models.JSONField(default=dict)
    member_invite = models.ForeignKey(
        MemberInvite,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
        help_text=_("Link to MemberInvite for member_invite notifications."),
        db_column="invite_id",  # Keep old column name for backward compatibility
    )
    guest_invite = models.ForeignKey(
        "workflows.GuestInvite",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
        help_text=_("Link to GuestInvite for guest_invite notifications."),
    )
    workflow_invite = models.ForeignKey(
        "workflows.WorkflowInvite",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="notifications",
        help_text=_("Link to WorkflowInvite for workflow_invite notifications."),
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
        # Non-invite notifications are always dismissible
        if self.type == self.Type.SYSTEM_ALERT:
            return True

        # Check member invite (MemberInvite)
        if self.member_invite:
            if self.user != self.member_invite.invitee_user:
                return True
            return self.member_invite.status != InviteStatus.PENDING

        # Check guest invite (GuestInvite)
        if self.guest_invite:
            if self.user != self.guest_invite.invitee_user:
                return True
            return self.guest_invite.status != InviteStatus.PENDING

        # Check workflow invite (WorkflowInvite)
        if self.workflow_invite:
            if self.user != self.workflow_invite.invitee_user:
                return True
            return self.workflow_invite.status != InviteStatus.PENDING

        # No linked invite - allow dismissal
        return True
