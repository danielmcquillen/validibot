from django.http import HttpRequest
from django.urls import reverse


def is_htmx(request: HttpRequest) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def reverse_with_org(
    viewname: str,
    *,
    request: HttpRequest | None = None,
    args: tuple | None = None,
    kwargs: dict | None = None,
):
    """Wrapper around :func:`django.urls.reverse` for future org-aware routing."""

    if kwargs is None:
        kwargs = {}
    return reverse(viewname, args=args or (), kwargs=kwargs)


def get_request_ip(request: HttpRequest) -> str | None:
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR") or request.headers.get(
        "X-Forwarded-For"
    )
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    remote_addr = request.META.get("REMOTE_ADDR")
    if remote_addr:
        return remote_addr.strip()
    return None
