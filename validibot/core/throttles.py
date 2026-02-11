"""
Custom DRF throttling classes for rate limiting.

This module provides guest-aware throttling that applies different rate limits
to workflow guests vs regular organization members.
"""

from __future__ import annotations

import logging

from rest_framework.throttling import ScopedRateThrottle

logger = logging.getLogger(__name__)


class GuestAwareThrottle(ScopedRateThrottle):
    """
    A scoped throttle that applies different rate limits for workflow guests.

    Workflow guests (users with WorkflowAccessGrants but no org membership)
    get more restrictive rate limits than regular organization members.

    Usage:
        Set `throttle_scope = "workflow_launch"` on a view.
        This throttle will check for a `guest_workflow_launch` rate if the
        user is a workflow guest, falling back to the normal scope rate.

    Settings example:
        REST_FRAMEWORK = {
            "DEFAULT_THROTTLE_RATES": {
                "workflow_launch": "60/minute",
                "guest_workflow_launch": "20/minute",
            }
        }
    """

    def get_cache_key(self, request, view):
        """
        Return a cache key that includes guest status for differentiated limits.

        For guests, we prefix the scope with 'guest_' to look up a separate rate.
        """
        if not request.user or not request.user.is_authenticated:
            return None  # Let AnonRateThrottle handle anonymous users

        # Check if user is a workflow guest
        is_guest = getattr(request.user, "is_workflow_guest", False)

        if is_guest:
            # Try to use a guest-specific scope
            guest_scope = f"guest_{self.scope}"
            if guest_scope in self.THROTTLE_RATES:
                self.scope = guest_scope
                self.rate = self.get_rate()
                self.num_requests, self.duration = self.parse_rate(self.rate)

        return super().get_cache_key(request, view)
