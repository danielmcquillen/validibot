import logging

from django.conf import settings

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
        }
    except Exception:
        # In case settings are not properly configured yet
        logger.warning("Could not load site feature settings")

    return extra_context
