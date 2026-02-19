import logging

from django.conf import settings

from validibot.core.features import get_feature_context
from validibot.core.license import get_license

logger = logging.getLogger(__name__)


def site_feature_settings(request):
    """Expose some settings from django-allauth in templates."""

    try:
        return {
            "ACCOUNT_ALLOW_LOGIN": settings.ACCOUNT_ALLOW_LOGIN,
            "ACCOUNT_ALLOW_REGISTRATION": settings.ACCOUNT_ALLOW_REGISTRATION,
            "ENABLE_APP": settings.ENABLE_APP,
            "ENABLE_API": settings.ENABLE_API,
        }
    except Exception:
        # In case settings are not properly configured yet
        logger.warning("Could not load site feature settings")
        return {}


def license_context(request):
    """
    Expose license edition information in templates.

    Provides:
    - license_edition: The current Edition enum value
    - is_enterprise: True if Enterprise edition
    - is_pro: True if Pro or Enterprise edition
    - is_community: True if Community edition
    """
    lic = get_license()
    return {
        "license_edition": lic.edition,
        "is_enterprise": lic.is_enterprise,
        "is_pro": lic.is_pro,
        "is_community": lic.is_community,
    }


def features_context(request):
    """
    Expose commercial feature flags in templates.

    Commercial packages (validibot-pro, validibot-enterprise) register
    the features they enable. This context processor makes those flags
    available in templates.

    Provides feature_<name> boolean flags:
    - feature_team_management: Team/member management (Pro)
    - feature_guest_management: Guest user management (Pro)
    - feature_billing: Billing/Stripe integration (Pro)
    - feature_advanced_analytics: Advanced analytics (Pro)
    - feature_multi_org: Multi-organization support (Enterprise)
    - etc.
    """
    return get_feature_context()
