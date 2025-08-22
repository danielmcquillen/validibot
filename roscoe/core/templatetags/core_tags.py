import logging
from typing import Optional

from django import template
from django.contrib.sites.shortcuts import get_current_site

logger = logging.getLogger(__name__)


register = template.Library()


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
        attribute = "active" if request.path.startswith(f"/{nav_item_name}/") else ""
        return attribute
    return ""


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
