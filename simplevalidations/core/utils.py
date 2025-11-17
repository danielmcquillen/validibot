import json

import bleach
import markdown2
from django.http import HttpRequest
from django.urls import reverse
from lxml import etree


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


def pretty_xml(text: str) -> str:
    """
    Safely pretty-print user-supplied XML for display.
    """
    if not text:
        return ""
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(
        text.encode("utf-8"),
        parser=parser,
    )  # may raise; handle errors
    pretty = etree.tostring(root, encoding="unicode", pretty_print=True)
    return pretty


def pretty_json(text: str) -> str:
    """
    Safely pretty-print user-supplied JSON for display.
    Returns a string that is ready to be escaped in the template.
    """
    if text in (None, "", {}):
        return ""
    try:
        # Already structured? Just pretty dump it.
        if not isinstance(text, (str, bytes, bytearray)):
            obj = text
        else:
            obj = json.loads(text)
        formatted = json.dumps(
            obj,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
    except Exception:
        # If invalid JSON, just return the raw text
        formatted = str(text).strip()
    return formatted


def truthy(value: str | None) -> bool:
    return str(value).lower() in {"1", "true", "yes", "on"}
