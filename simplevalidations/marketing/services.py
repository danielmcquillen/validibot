from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import send_mail
from django.core.validators import URLValidator
from django.core.validators import validate_ipv46_address
from django.utils import timezone
from django.utils.translation import gettext

from simplevalidations.marketing.constants import ProspectEmailStatus
from simplevalidations.marketing.constants import ProspectOrigins
from simplevalidations.marketing.models import Prospect

logger = logging.getLogger(__name__)


class WaitlistSignupError(Exception):
    """Raised when we cannot store a prospect or send the welcome email."""


@dataclass(frozen=True)
class WaitlistPayload:
    email: str
    metadata: dict[str, Any]


default_plain_waitlist_msg = gettext(
    """
Hey there — this is Daniel from SimpleValidations.
Thanks for joining the SimpleValidations beta list!
I'm working hard to get the beta release ready. I'll email you as soon as invites open.

— Daniel

{{{ pm:unsubscribe }}}
""",
).strip()

default_html_waitlist_msg = gettext(
    """
<p>Hey there — this is Daniel from SimpleValidations. Thanks for joining the
 SimpleValidations beta list! 🎉</p>
<p>I'm working hard to get the beta release ready. I'll email you as
 soon as invites open.</p>
<p>— Daniel</p>
<br/>
{{{ pm:unsubscribe }}}
""",
).strip()


def submit_waitlist_signup(payload: WaitlistPayload) -> None:  # noqa: PLR0912, PLR0915
    """
    Persist a beta waitlist signup locally and send a transactional welcome email.

    Parameters
    ----------
    payload:
        WaitlistPayload containing the email we collected as well as metadata that helps
        enrich the local Prospect record (user agent, source, etc).
    """

    metadata = dict(payload.metadata or {})
    skip_email = bool(metadata.pop("skip_email", False))
    origin = metadata.get("origin") or ProspectOrigins.HERO
    if origin not in ProspectOrigins.values:
        origin = ProspectOrigins.HERO

    source = (metadata.get("source") or "")[:100]
    referer = (metadata.get("referer") or "")[:500]
    user_agent = metadata.get("user_agent") or ""
    ip_address = metadata.get("ip") or None

    if referer:
        validator = URLValidator()
        try:
            validator(referer)
        except ValidationError:
            referer = ""

    if ip_address:
        try:
            validate_ipv46_address(ip_address)
        except ValidationError:
            ip_address = None

    prospect_defaults = {
        "origin": origin,
        "source": source,
        "referer": referer,
        "user_agent": user_agent,
        "ip_address": ip_address,
        "email_status": ProspectEmailStatus.PENDING,
    }

    prospect, created = Prospect.objects.get_or_create(
        email=payload.email,
        defaults=prospect_defaults,
    )

    updated_fields: list[str] = []
    if not created:
        for field, value in prospect_defaults.items():
            if value and getattr(prospect, field) != value:
                setattr(prospect, field, value)
                updated_fields.append(field)

    if updated_fields:
        prospect.save(update_fields=updated_fields)

    subject = metadata.get(
        "subject",
        gettext("You're on the SimpleValidations beta list!"),
    )

    welcome_txt = metadata.get("message", default_plain_waitlist_msg)
    welcome_html = metadata.get("message_html", default_html_waitlist_msg)

    if not skip_email:
        from_email = getattr(settings, "DEFAULT_FROM_EMAIL", None)
        try:
            sent = send_mail(
                subject,
                welcome_txt,
                from_email,
                [payload.email],
                html_message=welcome_html,
            )
        except (
            Exception
        ) as exc:  # pragma: no cover - send_mail errors are rare but critical
            logger.exception("Error sending waitlist welcome email.")
            err = gettext("Unable to send the welcome email.")
            raise WaitlistSignupError(err) from exc

        if sent == 0:
            logger.error(
                "Postmark (via Anymail) did not accept the welcome email for %s.",
                payload.email,
            )
            raise WaitlistSignupError(
                gettext("Postmark did not accept the welcome email."),
            )

        fields_to_update: list[str] = []
        if prospect.email_status != ProspectEmailStatus.PENDING:
            prospect.email_status = ProspectEmailStatus.PENDING
            fields_to_update.append("email_status")
        if not prospect.welcome_sent_at:
            prospect.welcome_sent_at = timezone.now()
            fields_to_update.append("welcome_sent_at")
        if fields_to_update:
            prospect.save(update_fields=fields_to_update)

    logger.info("Stored prospect %s and sent welcome email.", payload.email)
