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
        }
    except Exception:
        # In case settings are not properly configured yet
        logger.warning("Could not load site feature settings")

    return extra_context
