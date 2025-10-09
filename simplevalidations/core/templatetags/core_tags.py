import logging
from typing import Optional

from django import template
from django.contrib.sites.shortcuts import get_current_site

from simplevalidations.core.utils import reverse_with_org

logger = logging.getLogger(__name__)


register = template.Library()


@register.filter
def contrast_color(hex_color: str) -> str:
    """Given a hex color string, return either black or white depending on contrast."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return "#000000"  # Default to black if invalid

    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    brightness = (r * 299 + g * 587 + b * 114) / 1000
    return "#000000" if brightness > 128 else "#FFFFFF"


@register.simple_tag(takes_context=True)
def site_name(context) -> Optional[str]:
    request = context.get("request", None)
    if request:
        site = get_current_site(request)
        if site.name:
            return site.name
        else:
            return "(no site name defined)"
    else:
        logger.exception("site is not defined.")
    return None


@register.simple_tag(takes_context=True)
def active_link(context, nav_item_name):
    request = context.get("request", None)
    if request:
        return "active" if request.path.startswith(f"/app/{nav_item_name}/") else ""
    return ""


@register.simple_tag(takes_context=True)
def active_link_any(context, *nav_item_names):
    request = context.get("request", None)
    if not request:
        return ""
    for name in nav_item_names:
        if not name:
            continue
        prefix = f"/{name.strip('/')}/"
        if request.path.startswith(prefix):
            return "active"
    return ""


@register.simple_tag(takes_context=True)
def user_settings_nav_state(context) -> dict[str, bool]:
    """Return navigation state booleans for the user settings menu."""

    request = context.get("request")
    state = {
        "active": False,
        "profile": False,
        "email": False,
        "api_key": False,
    }
    if not request:
        return state

    match = getattr(request, "resolver_match", None)
    if not match:
        return state

    url_name = (getattr(match, "url_name", "") or "").strip()
    view_name = (getattr(match, "view_name", "") or "").strip()

    def is_match(name: str) -> bool:
        return url_name == name or view_name == f"users:{name}"

    state["profile"] = is_match("profile")
    state["email"] = is_match("email")
    state["api_key"] = is_match("api-key")
    state["active"] = any(state[key] for key in ("profile", "email", "api_key"))
    return state


@register.simple_tag(takes_context=True)
def active_builder_link(context, nav_item_name):
    request = context.get("request", None)
    if request:
        request = context["request"]
        return "active" if request.path.startswith(f"/builder/{nav_item_name}/") else ""
    return ""


@register.filter
def get_item(mapping, key):
    return mapping.get(key)


@register.simple_tag(takes_context=True)
def org_url(context, view_name, *args, **kwargs):
    request = context.get("request")
    resolved_kwargs = dict(kwargs or {})
    return reverse_with_org(
        view_name, request=request, args=args, kwargs=resolved_kwargs
    )


@register.simple_tag
def marketing_waitlist_form(origin: str = "hero"):
    from simplevalidations.marketing.forms import BetaWaitlistForm

    value = origin.strip().lower() if origin else BetaWaitlistForm.ORIGIN_HERO
    if value not in BetaWaitlistForm.ALLOWED_ORIGINS:
        value = BetaWaitlistForm.ORIGIN_HERO
    return BetaWaitlistForm(origin=value)
