import logging

from django.conf import settings

from validibot.core.license import get_license

logger = logging.getLogger(__name__)


def site_feature_settings(request):
    """Expose some settings from django-allauth in templates."""

    extra_context = {}

    try:
        enable_free_trial_signup = (
            settings.ENABLE_FREE_TRIAL_SIGNUP
            and request.user
            and request.user.is_authenticated
        )
        extra_context = {
            "ENABLE_FREE_TRIAL_SIGNUP": enable_free_trial_signup,
            "ENABLE_SYSTEM_STATUS_PAGE": settings.ENABLE_SYSTEM_STATUS_PAGE,
            "ENABLE_RESOURCES_SECTION": settings.ENABLE_RESOURCES_SECTION,
            "ENABLE_DOCS_SECTION": settings.ENABLE_DOCS_SECTION,
            "ENABLE_PRICING_SECTION": settings.ENABLE_PRICING_SECTION,
            "ENABLE_FEATURES_SECTION": settings.ENABLE_FEATURES_SECTION,
            "ENABLE_BLOG": settings.ENABLE_BLOG,
            "ENABLE_HELP_CENTER": settings.ENABLE_HELP_CENTER,
            "ENABLE_SYSTEM_STATUS": settings.ENABLE_SYSTEM_STATUS,
            "ENABLE_AI_VALIDATIONS": settings.ENABLE_AI_VALIDATIONS,
            "ACCOUNT_ALLOW_LOGIN": settings.ACCOUNT_ALLOW_LOGIN,
            "ACCOUNT_ALLOW_REGISTRATION": settings.ACCOUNT_ALLOW_REGISTRATION,
            "ENABLE_APP": settings.ENABLE_APP,
            "ENABLE_API": settings.ENABLE_API,
        }
    except Exception:
        # In case settings are not properly configured yet
        logger.warning("Could not load site feature settings")

    return extra_context


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
