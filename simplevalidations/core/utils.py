import bleach
import markdown2
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
        "X-Forwarded-For",
    )
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    remote_addr = request.META.get("REMOTE_ADDR")
    if remote_addr:
        return remote_addr.strip()
    return None


ALLOWED_TAGS = [
    # structure
    "p",
    "br",
    "hr",
    "blockquote",
    "pre",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "li",
    "dl",
    "dt",
    "dd",
    # inline
    "a",
    "strong",
    "em",
    "code",
    "kbd",
    "samp",
    "sub",
    "sup",
    "span",
    # tables & images (if you want them)
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "img",
]
ALLOWED_ATTRS = {
    "a": ["href", "title", "target", "rel"],
    "img": ["src", "alt", "title", "width", "height"],
    "th": ["colspan", "rowspan"],
    "td": ["colspan", "rowspan"],
    "span": ["class"],
    "code": ["class"],
}
ALLOWED_PROTOCOLS = ["http", "https", "mailto"]


def render_markdown_safe(text_md: str) -> str:
    html = markdown2.markdown(
        text_md or "",
        extras=[
            "fenced-code-blocks",
            "tables",
            "strike",
            "cuddled-lists",
            "link-patterns",
        ],
    )
    # First pass: clean
    html = bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        protocols=ALLOWED_PROTOCOLS,
        strip=True,
    )

    # Second pass: linkify + safe target/rel
    def set_target(attrs, new=False):
        href = attrs.get("href", "")
        if href.startswith(("http://", "https://")):
            attrs["target"] = "_blank"
            attrs["rel"] = "noopener nofollow ugc"
        return attrs

    html = bleach.linkify(html, callbacks=[set_target])
    return html
