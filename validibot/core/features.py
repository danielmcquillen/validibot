"""
Feature-flag accessors for Validibot commercial features.

The actual list of enabled features is carried on the active
:class:`~validibot.core.license.License` object — community code
never talks to a separate feature registry. This module provides
the friendly readers that gate UI elements and code paths:

- :class:`CommercialFeature` — the canonical enum of every feature
  the platform knows about. Community uses this for gating
  (``FeatureRequiredMixin``, template ``{% if feature_X %}``)
  without needing to know which commercial package owns which
  feature.
- :func:`is_feature_enabled` — ``True`` if the feature is in the
  current license's feature set.
- :func:`get_feature_context` — dict of ``feature_<name>: bool``
  keys for the template context processor.

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

from django.db.models import TextChoices
from django.utils.translation import gettext_lazy as _

from validibot.core.license import get_license


class CommercialFeature(TextChoices):
    """
    Commercial features requiring validibot-pro or validibot-enterprise.

    Pro features are also available in Enterprise tier. The enum is
    the canonical, community-visible list of feature names so gating
    code can reference ``CommercialFeature.TEAM_MANAGEMENT`` rather
    than a raw string — typos become type errors.
    """

    # Pro features (requires validibot-pro, also available in Enterprise)
    TEAM_MANAGEMENT = "team_management", _("Team Management")
    GUEST_MANAGEMENT = "guest_management", _("Guest Management")
    BILLING = "billing", _("Billing")
    ADVANCED_ANALYTICS = "advanced_analytics", _("Advanced Analytics")
    SIGNED_CREDENTIALS = "signed_credentials", _("Signed Credentials")

    # Enterprise features (requires validibot-enterprise)
    MULTI_ORG = "multi_org", _("Multiple Organizations")
    LDAP_INTEGRATION = "ldap_integration", _("LDAP Integration")
    SAML_SSO = "saml_sso", _("SAML Single Sign-On")


def is_feature_enabled(feature: str | CommercialFeature) -> bool:
    """Return ``True`` if *feature* is enabled by the current license.

    Accepts either a :class:`CommercialFeature` enum member or its
    raw string value — both equate to the same underlying string
    thanks to ``TextChoices``.
    """
    # Normalise enum members to their string value so frozenset
    # membership works identically for both call styles.
    key = feature.value if isinstance(feature, CommercialFeature) else feature
    return key in get_license().features


def get_feature_context() -> dict[str, bool]:
    """Build a template-context dict of ``feature_<name>: bool`` keys.

    Iterates every :class:`CommercialFeature` so templates can rely
    on ``feature_foo`` existing (as ``False``) even when the feature
    isn't enabled — avoids ``{% if feature_foo %}`` tripping over a
    missing key.
    """
    enabled = get_license().features
    return {
        f"feature_{feature.value}": feature.value in enabled
        for feature in CommercialFeature
    }
