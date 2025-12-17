"""
Billing middleware for subscription enforcement.

This middleware checks subscription status on each request and blocks
users with expired trials from accessing the app or API.
"""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING

from django.http import JsonResponse
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
    Block users with expired trials from accessing app and API.

    Checks subscription status on each request. If trial has expired:
    - Web requests: Redirect to /app/billing/trial-expired/
    - API requests: Return 402 Payment Required JSON response

    This middleware should be added after AuthenticationMiddleware.
    """

    # Paths that don't require an active subscription
    EXEMPT_PATH_PREFIXES = [
        # Billing and payment
        "/app/billing/",
        "/billing/",
        "/stripe/",
        # Authentication
        "/accounts/",
        # Static files
        "/static/",
        "/media/",
        # Admin
        "/admin/",
        "/.well-known/",
        # Marketing pages - allow trial-expired users to browse the site
        "/about/",
        "/pricing/",
        "/features/",
        "/contact/",
        "/terms/",
        "/privacy/",
        "/blog/",
        "/support/",
        "/help/",
        "/resources/",
        "/waitlist/",
        "/webhooks/",
    ]

    # Exact paths that are exempt (for home page)
    EXEMPT_EXACT_PATHS = ["/"]

    # Blocked subscription statuses
    BLOCKED_STATUSES = {
        SubscriptionStatus.TRIAL_EXPIRED,
        SubscriptionStatus.SUSPENDED,
        SubscriptionStatus.CANCELED,
    }

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
                # Trial has expired - update status
                subscription.status = SubscriptionStatus.TRIAL_EXPIRED
                subscription.save(update_fields=["status"])
                logger.info(
                    "Trial expired for org=%s",
                    org.id,
                )

        # Block if subscription is in a blocked status
        if subscription.status in self.BLOCKED_STATUSES:
            return self._block_request(request, subscription.status)

        return self.get_response(request)

    def _is_exempt_path(self, path: str) -> bool:
        """Check if the path is exempt from subscription checks."""
        # Check exact path matches first (for home page)
        if path in self.EXEMPT_EXACT_PATHS:
            return True
        # Check prefix matches
        return any(path.startswith(prefix) for prefix in self.EXEMPT_PATH_PREFIXES)

    def _is_api_request(self, request: HttpRequest) -> bool:
        """Check if this is an API request."""
        return request.path.startswith("/api/")

    def _block_request(
        self,
        request: HttpRequest,
        status: str,
    ) -> HttpResponse:
        """Block the request based on subscription status."""
        if self._is_api_request(request):
            # Return JSON error for API requests
            error_messages = {
                SubscriptionStatus.TRIAL_EXPIRED: (
                    "Your trial has expired. Please subscribe to continue."
                ),
                SubscriptionStatus.SUSPENDED: (
                    "Your subscription is suspended. "
                    "Please update your payment method."
                ),
                SubscriptionStatus.CANCELED: (
                    "Your subscription has been canceled. "
                    "Please resubscribe to continue."
                ),
            }
            return JsonResponse(
                {
                    "detail": error_messages.get(status, "Subscription inactive."),
                    "code": "subscription_inactive",
                    "status": status,
                },
                status=HTTPStatus.PAYMENT_REQUIRED,
            )
        # Redirect web requests to the conversion page
        return redirect("billing:trial-expired")
