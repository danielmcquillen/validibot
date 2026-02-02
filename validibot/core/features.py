"""
Feature registry for Validibot commercial features.

This module provides a simple registry pattern for commercial packages
(validibot-pro, validibot-enterprise) to register the features they enable.

Commercial packages register features at import time, and the core application
checks feature availability to gate UI elements and API endpoints.

Usage in commercial packages::

    # In validibot_enterprise/__init__.py
    from validibot.core.features import register_feature, FEATURE_MULTI_ORG

    register_feature(FEATURE_MULTI_ORG)
    register_feature(FEATURE_GUEST_MANAGEMENT)

Usage in templates::

    {% if feature_multi_org %}
        <a href="...">Manage organizations</a>
    {% endif %}

Usage in views::

    from validibot.core.features import is_feature_enabled, FEATURE_MULTI_ORG

    if is_feature_enabled(FEATURE_MULTI_ORG):
        # Show multi-org UI
        ...

"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# Feature constants
# Use these constants rather than string literals for type safety

# Pro features (also available in Enterprise)
FEATURE_MULTI_ORG = "multi_org"
FEATURE_BILLING = "billing"
FEATURE_ADVANCED_ANALYTICS = "advanced_analytics"
FEATURE_SIGNED_BADGES = "signed_badges"

# Enterprise features
FEATURE_GUEST_MANAGEMENT = "guest_management"
FEATURE_LDAP_INTEGRATION = "ldap_integration"
FEATURE_SAML_SSO = "saml_sso"
FEATURE_TEAM_MANAGEMENT = "team_management"


# Internal registry - commercial packages add features here
_enabled_features: set[str] = set()


def register_feature(feature_name: str) -> None:
    """
    Register a feature as enabled.

    Called by commercial packages when they're imported to indicate
    which features they provide.

    Args:
        feature_name: The feature constant (e.g., FEATURE_MULTI_ORG)
    """
    _enabled_features.add(feature_name)
    logger.info("Feature registered: %s", feature_name)


def unregister_feature(feature_name: str) -> None:
    """
    Unregister a feature (primarily for testing).

    Args:
        feature_name: The feature constant to unregister
    """
    _enabled_features.discard(feature_name)


def is_feature_enabled(feature_name: str) -> bool:
    """
    Check if a feature is enabled.

    Args:
        feature_name: The feature constant to check

    Returns:
        True if the feature has been registered by a commercial package
    """
    return feature_name in _enabled_features


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
        # Pro features
        "feature_multi_org": is_feature_enabled(FEATURE_MULTI_ORG),
        "feature_billing": is_feature_enabled(FEATURE_BILLING),
        "feature_advanced_analytics": is_feature_enabled(FEATURE_ADVANCED_ANALYTICS),
        "feature_signed_badges": is_feature_enabled(FEATURE_SIGNED_BADGES),
        # Enterprise features
        "feature_guest_management": is_feature_enabled(FEATURE_GUEST_MANAGEMENT),
        "feature_ldap_integration": is_feature_enabled(FEATURE_LDAP_INTEGRATION),
        "feature_saml_sso": is_feature_enabled(FEATURE_SAML_SSO),
        "feature_team_management": is_feature_enabled(FEATURE_TEAM_MANAGEMENT),
    }
