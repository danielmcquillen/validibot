from __future__ import annotations

from typing import Final

from django.conf import settings
from django import template
from django.template import TemplateSyntaxError


register = template.Library()

_SETTING_LOOKUP: Final[dict[str, str]] = {
    "resources": "RESOURCES_ENABLED",
    "docs": "DOCS_ENABLED",
    "pricing": "PRICING_ENABLED",
    "features": "FEATURES_ENABLED",
}


@register.simple_tag
def marketing_feature_enabled(feature_key: str) -> bool:
    """
    Resolve marketing feature toggles based on settings.

    Usage:
        {% load marketing_flags %}
        {% marketing_feature_enabled "resources" as resources_enabled %}
        {% if resources_enabled %}
            ...
        {% endif %}

    Args:
        feature_key: One of "resources", "docs", "pricing", or "features".

    Raises:
        TemplateSyntaxError: If the feature_key is not recognised.
    """

    normalized_key = (feature_key or "").strip().lower()
    setting_name = _SETTING_LOOKUP.get(normalized_key)
    if setting_name is None:
        raise TemplateSyntaxError(
            f"Unknown marketing feature '{feature_key}'. "
            "Expected one of: resources, docs, pricing, features."
        )
    return bool(getattr(settings, setting_name, False))
