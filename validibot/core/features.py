"""
Feature registry for Validibot commercial features.

This module provides a simple registry pattern for commercial packages
(validibot-pro, validibot-enterprise) to register the features they enable.

Commercial packages register features at import time, and the core application
checks feature availability to gate UI elements and API endpoints.

Usage in commercial packages::

    # In validibot_pro/__init__.py
    from validibot.core.features import register_feature, CommercialFeature

    register_feature(CommercialFeature.TEAM_MANAGEMENT)

    # In validibot_enterprise/__init__.py
    from validibot.core.features import register_feature, CommercialFeature

    register_feature(CommercialFeature.MULTI_ORG)

Usage in templates::

    {% if feature_multi_org %}
        <a href="...">Manage organizations</a>
    {% endif %}

Usage in views::

    from validibot.core.features import is_feature_enabled, CommercialFeature

    if is_feature_enabled(CommercialFeature.MULTI_ORG):
        # Show multi-org UI
        ...

"""

from __future__ import annotations

import logging

from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _

logger = logging.getLogger(__name__)


class CommercialFeature(TextChoices):
    """
    Commercial features requiring validibot-pro or validibot-enterprise.

    Pro features are also available in Enterprise tier.
    """

    # Pro features (requires validibot-pro, also available in Enterprise)
    TEAM_MANAGEMENT = "team_management", _("Team Management")
    BILLING = "billing", _("Billing")
    ADVANCED_ANALYTICS = "advanced_analytics", _("Advanced Analytics")
    SIGNED_BADGES = "signed_badges", _("Signed Badges")

    # Enterprise features (requires validibot-enterprise)
    MULTI_ORG = "multi_org", _("Multiple Organizations")
    GUEST_MANAGEMENT = "guest_management", _("Guest Management")
    LDAP_INTEGRATION = "ldap_integration", _("LDAP Integration")
    SAML_SSO = "saml_sso", _("SAML Single Sign-On")


# Internal registry - commercial packages add features here
_enabled_features: set[str] = set()


def register_feature(feature: str) -> None:
    """
    Register a feature as enabled.

    Called by commercial packages when they're imported to indicate
    which features they provide.

    Args:
        feature: The feature value (e.g., CommercialFeature.TEAM_MANAGEMENT)
    """
    _enabled_features.add(feature)
    logger.info("Feature registered: %s", feature)


def unregister_feature(feature: str) -> None:
    """
    Unregister a feature (primarily for testing).

    Args:
        feature: The feature value to unregister
    """
    _enabled_features.discard(feature)


def is_feature_enabled(feature: str) -> bool:
    """
    Check if a feature is enabled.

    Args:
        feature: The feature value to check

    Returns:
        True if the feature has been registered by a commercial package
    """
    return feature in _enabled_features


def get_enabled_features() -> frozenset[str]:
    """
    Get all currently enabled features.

    Returns:
        Frozen set of enabled feature names
    """
    return frozenset(_enabled_features)


def reset_features() -> None:
    """
    Clear all registered features (for testing).
    """
    _enabled_features.clear()


def get_feature_context() -> dict[str, bool]:
    """
    Get a dictionary of feature flags for template context.

    Returns a dict with feature_<name> keys for each possible feature,
    with boolean values indicating whether each is enabled.

    Returns:
        Dictionary suitable for template context
    """
    return {
        f"feature_{feature.value}": is_feature_enabled(feature)
        for feature in CommercialFeature
    }


def get_feature_label(feature: str) -> str:
    """
    Get the human-readable label for a feature.

    Useful for displaying feature names in upgrade prompts.

    Args:
        feature: The feature value

    Returns:
        The translated label for the feature
    """
    for choice in CommercialFeature:
        if choice.value == feature:
            return str(choice.label)
    return feature
