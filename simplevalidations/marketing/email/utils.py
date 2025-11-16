from __future__ import annotations

import base64
import hashlib
import hmac

from django.conf import settings
from django.http import HttpRequest

from simplevalidations.core.utils import get_request_ip


def is_allowed_postmark_source(request: HttpRequest) -> bool:
    """
    Validate inbound Postmark webhook traffic.

    Priority order:
    1. If POSTMARK_WEBHOOK_SIGNING_SECRET is set, require a valid HMAC signature.
    2. Otherwise fall back to strict REMOTE_ADDR allowlist checks (ignore
       spoofable forwarding headers).
    """

    secret = getattr(settings, "POSTMARK_WEBHOOK_SIGNING_SECRET", "")
    signature = request.headers.get("X-Postmark-Signature", "")

    if secret:
        if not signature:
            return False
        expected = hmac.new(
            key=secret.encode("utf-8"),
            msg=request.body or b"",
            digestmod=hashlib.sha256,
        ).digest()
        calculated = base64.b64encode(expected).decode("utf-8")
        return hmac.compare_digest(signature.strip(), calculated)

    allowed_ips = getattr(settings, "POSTMARK_WEBHOOK_ALLOWED_IPS", [])
    if not allowed_ips:
        return True
    client_ip = request.META.get("REMOTE_ADDR") or get_request_ip(request)
    if not client_ip:
        return False
    return client_ip in allowed_ips
