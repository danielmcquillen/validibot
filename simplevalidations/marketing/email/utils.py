from django.conf import settings
from django.http import HttpRequest

from simplevalidations.core.utils import get_request_ip


def is_allowed_postmark_source(request: HttpRequest) -> bool:
    allowed_ips = getattr(settings, "POSTMARK_WEBHOOK_ALLOWED_IPS", [])
    if not allowed_ips:
        return True
    client_ip = get_request_ip(request)
    if not client_ip:
        return False
    return client_ip in allowed_ips
