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
