"""
Context processors for billing information.

Provides trial status and subscription info to all templates.
"""

from __future__ import annotations

import logging

from django.utils import timezone

from validibot.billing.constants import SubscriptionStatus

logger = logging.getLogger(__name__)


def trial_banner_context(request):
    """
    Provide trial status info for the persistent trial banner.

    Returns context with:
    - show_trial_banner: Whether to show the banner
    - trial_days_remaining: Days left in trial
    - trial_plan_name: Name of the plan they're trialing
    - subscription_status: Current subscription status

    Only provides data for authenticated users with an active org.
    """
    context = {
        "show_trial_banner": False,
        "trial_days_remaining": 0,
        "trial_plan_name": "",
        "subscription_status": None,
    }

    # Only for authenticated users
    if not hasattr(request, "user") or not request.user.is_authenticated:
        return context

    # Need an active org with subscription
    org = getattr(request, "active_org", None)
    if not org:
        org = getattr(request.user, "current_org", None)
    if not org:
        return context

    subscription = getattr(org, "subscription", None)
    if not subscription:
        return context

    context["subscription_status"] = subscription.status

    # Show banner for trialing users
    if subscription.status == SubscriptionStatus.TRIALING:
        context["show_trial_banner"] = True
        context["trial_plan_name"] = subscription.plan.name if subscription.plan else ""

        if subscription.trial_ends_at:
            delta = subscription.trial_ends_at - timezone.now()
            context["trial_days_remaining"] = max(0, delta.days)

    return context
