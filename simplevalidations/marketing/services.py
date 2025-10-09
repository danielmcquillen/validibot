from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)


class WaitlistSignupError(Exception):
    """Raised when we cannot record a waitlist signup with Sentry."""


@dataclass(frozen=True)
class WaitlistPayload:
    email: str
    metadata: dict[str, Any]


def submit_waitlist_signup(payload: WaitlistPayload) -> None:
    """
    Persist a beta waitlist signup using the configured Sentry Automation endpoint.

    Parameters
    ----------
    payload:
        WaitlistPayload containing the email we collected as well as metadata that helps
        drive the automation in Sentry (user agent, source, etc).
    """

    api_url = getattr(settings, "SENTRY_WAITLIST_API_URL", None)
    api_key = getattr(settings, "SENTRY_API_KEY", None)

    if not api_url or not api_key:
        logger.warning(
            "Skipping waitlist submission because Sentry configuration is incomplete.",
        )
        raise WaitlistSignupError("Sentry waitlist integration is not configured.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    try:
        response = httpx.post(
            api_url,
            headers=headers,
            content=json.dumps(
                {
                    "email": payload.email,
                    "metadata": payload.metadata,
                },
            ),
            timeout=getattr(settings, "HTTP_DEFAULT_TIMEOUT", 10),
        )
    except httpx.HTTPError as exc:  # pragma: no cover - network errors are hard to simulate
        logger.exception("Error calling Sentry Automation endpoint.")
        raise WaitlistSignupError("Unable to reach Sentry Automation.") from exc

    if response.status_code >= 300:
        logger.error(
            "Sentry Automation rejected waitlist signup: status=%s body=%s",
            response.status_code,
            response.text,
        )
        raise WaitlistSignupError("Sentry Automation rejected the waitlist request.")

    logger.info("Recorded beta waitlist signup with Sentry.")
