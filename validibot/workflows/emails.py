"""
Email notifications for workflow invites and access grants.

This module handles sending email notifications when:
- A workflow invite is created (to the invitee)
- A workflow invite is accepted (to the inviter)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils.translation import gettext as _

if TYPE_CHECKING:
    from validibot.workflows.models import WorkflowAccessGrant
    from validibot.workflows.models import WorkflowInvite

logger = logging.getLogger(__name__)


def get_site_url() -> str:
    """
    Get the base site URL for building absolute URLs in emails.

    Uses SITE_URL setting if available, falls back to default.
    """
    return getattr(settings, "SITE_URL", "https://validibot.com")


def send_workflow_invite_email(invite: WorkflowInvite) -> bool:
    """
    Send an email notification to the invitee about a workflow invite.

    The email contains:
    - Who sent the invite
    - Which workflow they're being invited to
    - A link to accept the invite

    Args:
        invite: The WorkflowInvite instance.

    Returns:
        True if email was sent successfully, False otherwise.
    """
    recipient_email = invite.invitee_email
    if not recipient_email:
        logger.warning(
            "Cannot send invite email: no invitee_email on invite %s",
            invite.id,
        )
        return False

    inviter_name = invite.inviter.get_full_name() or invite.inviter.username
    workflow_name = invite.workflow.name
    org_name = invite.workflow.org.name

    # Build the acceptance URL
    site_url = get_site_url()
    accept_path = reverse("workflow_invite_accept", kwargs={"token": invite.token})
    accept_url = f"{site_url}{accept_path}"

    subject = _("You've been invited to use %(workflow_name)s on Validibot") % {
        "workflow_name": workflow_name,
    }

    plain_message = _(
        """Hi there,

%(inviter_name)s has invited you to access the workflow "%(workflow_name)s"
from %(org_name)s on Validibot.

Click the link below to accept this invitation:
%(accept_url)s

This invitation will expire in 7 days.

If you weren't expecting this invitation, you can safely ignore this email.

Thanks,
The Validibot Team
"""
    ) % {
        "inviter_name": inviter_name,
        "workflow_name": workflow_name,
        "org_name": org_name,
        "accept_url": accept_url,
    }

    html_message = _(
        """<p>Hi there,</p>

<p><strong>%(inviter_name)s</strong> has invited you to access the workflow
"<strong>%(workflow_name)s</strong>" from %(org_name)s on Validibot.</p>

<p><a href="%(accept_url)s">Click here to accept this invitation</a></p>

<p>This invitation will expire in 7 days.</p>

<p>If you weren't expecting this invitation, you can safely ignore this email.</p>

<p>Thanks,<br>
The Validibot Team</p>
"""
    ) % {
        "inviter_name": inviter_name,
        "workflow_name": workflow_name,
        "org_name": org_name,
        "accept_url": accept_url,
    }

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None)

    try:
        sent = send_mail(
            subject,
            plain_message,
            from_email,
            [recipient_email],
            html_message=html_message,
        )
    except Exception:
        logger.exception("Error sending workflow invite email to %s", recipient_email)
        return False

    if sent == 0:
        logger.error(
            "Email backend did not accept workflow invite email for %s",
            recipient_email,
        )
        return False

    logger.info(
        "Sent workflow invite email to %s for workflow %s",
        recipient_email,
        invite.workflow.name,
    )
    return True


def send_workflow_invite_accepted_email(grant: WorkflowAccessGrant) -> bool:
    """
    Send an email notification to the inviter when their invite is accepted.

    Args:
        grant: The WorkflowAccessGrant created from accepting the invite.

    Returns:
        True if email was sent successfully, False otherwise.
    """
    # The inviter is stored in granted_by
    inviter = grant.granted_by
    if not inviter:
        logger.warning(
            "Cannot send acceptance email: no granted_by on grant %s",
            grant.id,
        )
        return False

    inviter_email = inviter.email
    if not inviter_email:
        logger.warning(
            "Cannot send acceptance email: inviter %s has no email",
            inviter.username,
        )
        return False

    grantee = grant.user
    grantee_name = grantee.get_full_name() or grantee.username
    workflow_name = grant.workflow.name

    subject = _("%(grantee_name)s accepted your workflow invitation") % {
        "grantee_name": grantee_name,
    }

    plain_message = _(
        """Hi %(inviter_name)s,

Good news! %(grantee_name)s has accepted your invitation to access the workflow
"%(workflow_name)s".

They can now execute validations using this workflow.

Thanks,
The Validibot Team
"""
    ) % {
        "inviter_name": inviter.get_full_name() or inviter.username,
        "grantee_name": grantee_name,
        "workflow_name": workflow_name,
    }

    html_message = _(
        """<p>Hi %(inviter_name)s,</p>

<p>Good news! <strong>%(grantee_name)s</strong> has accepted your invitation to access
the workflow "<strong>%(workflow_name)s</strong>".</p>

<p>They can now execute validations using this workflow.</p>

<p>Thanks,<br>
The Validibot Team</p>
"""
    ) % {
        "inviter_name": inviter.get_full_name() or inviter.username,
        "grantee_name": grantee_name,
        "workflow_name": workflow_name,
    }

    from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None)

    try:
        sent = send_mail(
            subject,
            plain_message,
            from_email,
            [inviter_email],
            html_message=html_message,
        )
    except Exception:
        logger.exception("Error sending invite acceptance email to %s", inviter_email)
        return False

    if sent == 0:
        logger.error(
            "Email backend did not accept invite acceptance email for %s",
            inviter_email,
        )
        return False

    logger.info(
        "Sent invite acceptance email to %s for workflow %s",
        inviter_email,
        workflow_name,
    )
    return True
