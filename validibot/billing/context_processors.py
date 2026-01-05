"""
Context processors for billing information.

Provides UI state flags and trial banner info to all templates via a single
consolidated context processor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.utils import timezone

from validibot.billing.constants import SubscriptionStatus

if TYPE_CHECKING:
    from django.http import HttpRequest

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppUIState:
    """
    UI state flags for the app based on subscription status.

    This dataclass provides a clean, typed interface for templates to determine
    what UI elements to show/hide based on the user's subscription state.

    The `restricted_mode` flag is the key indicator: when True, the user has
    limited access (trial expired, suspended, canceled) and should see a
    minimal UI focused on billing/subscription actions.

    Attributes:
        restricted_mode: User has blocked subscription, show minimal UI
        show_nav_toggle: Show the left nav expand/collapse button
        show_notifications: Show the notifications bell icon
        show_full_user_menu: Show all user menu items (profile, email, etc.)
        show_back_to_site_link: Show "Back to Validibot" link in nav
        show_trial_banner: Show the trial countdown banner
        trial_days_remaining: Days left in trial (0 if not trialing)
        trial_plan_name: Name of the plan being trialed (empty if not trialing)
        subscription_status: Current subscription status string
    """

    # UI visibility flags
    restricted_mode: bool = False
    show_nav_toggle: bool = False
    show_notifications: bool = False
    show_full_user_menu: bool = False
    show_back_to_site_link: bool = False

    # Trial banner info
    show_trial_banner: bool = False
    trial_days_remaining: int = 0
    trial_plan_name: str = ""
    subscription_status: str | None = None


# Subscription statuses that trigger restricted mode
_RESTRICTED_STATUSES = frozenset({
    SubscriptionStatus.TRIAL_EXPIRED,
    SubscriptionStatus.SUSPENDED,
    SubscriptionStatus.CANCELED,
})

# Singleton for unauthenticated/no-subscription users - all UI hidden
_UNAUTHENTICATED_UI_STATE = AppUIState()


def _get_org_subscription(request: HttpRequest):
    """
    Get the organization and subscription for the current request.

    Returns (org, subscription) tuple, either of which may be None.
    """
    if not hasattr(request, "user") or not request.user.is_authenticated:
        return None, None

    org = getattr(request, "active_org", None)
    if not org:
        org = getattr(request.user, "current_org", None)
    if not org:
        return None, None

    subscription = getattr(org, "subscription", None)
    return org, subscription


def billing_context(request: HttpRequest) -> dict:
    """
    Provide UI state flags and trial banner info based on subscription status.

    This is the single consolidated context processor for all billing-related
    template context. It provides:

    1. `app_ui` - AppUIState object with UI visibility flags
    2. Legacy variables for trial banner compatibility:
       - show_trial_banner
       - trial_days_remaining
       - trial_plan_name
       - subscription_status

    Superusers bypass all billing restrictions - they always see full UI
    regardless of subscription status.

    Usage in templates:
        {% if app_ui.show_notifications %}
            ... notification bell ...
        {% endif %}

        {% if app_ui.restricted_mode %}
            ... show minimal UI ...
        {% endif %}

        {% if show_trial_banner %}
            ... trial banner (legacy) ...
        {% endif %}

    Returns:
        dict with 'app_ui' and legacy trial banner variables
    """
    # Superusers bypass all billing restrictions
    is_superuser = (
        hasattr(request, "user")
        and request.user.is_authenticated
        and request.user.is_superuser
    )
    if is_superuser:
        return {
            "app_ui": AppUIState(
                restricted_mode=False,
                show_nav_toggle=True,
                show_notifications=True,
                show_full_user_menu=True,
                show_back_to_site_link=False,
                show_trial_banner=False,
            ),
            "show_trial_banner": False,
            "trial_days_remaining": 0,
            "trial_plan_name": "",
            "subscription_status": None,
        }

    _, subscription = _get_org_subscription(request)

    # Default state for unauthenticated users or users without subscription
    # All UI elements hidden - they shouldn't see app UI anyway
    if not subscription:
        return {
            "app_ui": _UNAUTHENTICATED_UI_STATE,
            # Legacy variables for trial_banner.html compatibility
            "show_trial_banner": False,
            "trial_days_remaining": 0,
            "trial_plan_name": "",
            "subscription_status": None,
        }

    status = subscription.status
    is_restricted = status in _RESTRICTED_STATUSES
    is_trialing = status == SubscriptionStatus.TRIALING

    # Calculate trial info
    show_trial_banner = False
    trial_days_remaining = 0
    trial_plan_name = ""

    if is_trialing:
        show_trial_banner = True
        trial_plan_name = subscription.plan.name if subscription.plan else ""
        if subscription.trial_ends_at:
            delta = subscription.trial_ends_at - timezone.now()
            trial_days_remaining = max(0, delta.days)

    # Build UI state
    ui_state = AppUIState(
        # UI visibility
        restricted_mode=is_restricted,
        show_nav_toggle=not is_restricted,
        show_notifications=not is_restricted,
        show_full_user_menu=not is_restricted,
        show_back_to_site_link=is_restricted,
        # Trial info
        show_trial_banner=show_trial_banner,
        trial_days_remaining=trial_days_remaining,
        trial_plan_name=trial_plan_name,
        subscription_status=status,
    )

    return {
        "app_ui": ui_state,
        # Legacy variables for trial_banner.html compatibility
        "show_trial_banner": show_trial_banner,
        "trial_days_remaining": trial_days_remaining,
        "trial_plan_name": trial_plan_name,
        "subscription_status": status,
    }
