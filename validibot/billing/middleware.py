"""
Billing middleware for subscription enforcement.

This middleware checks subscription status on each request and redirects
users with expired trials to the conversion page.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from django.shortcuts import redirect
from django.utils import timezone

from validibot.billing.constants import SubscriptionStatus
from validibot.users.scoping import ensure_active_org_scope

if TYPE_CHECKING:
    from django.http import HttpRequest
    from django.http import HttpResponse

logger = logging.getLogger(__name__)


class TrialExpiryMiddleware:
    """
    Redirect users with expired trials to the conversion page.

    Checks subscription status on each request. If trial has expired,
    redirects to /app/billing/trial-expired/ (except for billing URLs
    and other exempt paths).

    This middleware should be added after AuthenticationMiddleware.
    """

    # Paths that don't require an active subscription
    EXEMPT_PATH_PREFIXES = [
        "/app/billing/",
        "/billing/",
        "/stripe/",
        "/accounts/",
        "/static/",
        "/media/",
        "/admin/",
        "/api/",
        "/.well-known/",
    ]

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # Skip for unauthenticated users
        if not request.user.is_authenticated:
            return self.get_response(request)

        # Skip exempt paths
        if self._is_exempt_path(request.path):
            return self.get_response(request)

        # Use ensure_active_org_scope for consistent org resolution
        # This syncs request.active_org with session and user.current_org
        _, org, _ = ensure_active_org_scope(request)
        if not org:
            return self.get_response(request)

        # Check subscription status
        subscription = getattr(org, "subscription", None)
        if not subscription:
            # No subscription yet - allow access
            # This handles edge cases during org creation
            return self.get_response(request)

        # Check if trial has expired
        if subscription.status == SubscriptionStatus.TRIALING:
            trial_expired = (
                subscription.trial_ends_at
                and subscription.trial_ends_at < timezone.now()
            )
            if trial_expired:
                # Trial has expired - update status and redirect
                subscription.status = SubscriptionStatus.TRIAL_EXPIRED
                subscription.save(update_fields=["status"])
                logger.info(
                    "Trial expired for org=%s, redirecting to conversion page",
                    org.id,
                )

        # Redirect if trial expired
        if subscription.status == SubscriptionStatus.TRIAL_EXPIRED:
            return redirect("billing:trial-expired")

        # Also block suspended/canceled subscriptions
        blocked_statuses = {
            SubscriptionStatus.SUSPENDED,
            SubscriptionStatus.CANCELED,
        }
        if subscription.status in blocked_statuses:
            return redirect("billing:trial-expired")

        return self.get_response(request)

    def _is_exempt_path(self, path: str) -> bool:
        """Check if the path is exempt from subscription checks."""
        return any(path.startswith(prefix) for prefix in self.EXEMPT_PATH_PREFIXES)
