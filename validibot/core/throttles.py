"""Custom DRF throttling classes for rate limiting.

This module provides kind-aware throttling that applies different rate
limits to GUEST-classified accounts vs. regular accounts. GUEST is a
system-wide :class:`~validibot.users.constants.UserKindGroup`, not a
per-workflow concept; this throttle keys off the account kind, not the
specific resource being accessed.
"""

from __future__ import annotations

import logging

from rest_framework.throttling import ScopedRateThrottle

logger = logging.getLogger(__name__)


class GuestAwareThrottle(ScopedRateThrottle):
    """A scoped throttle that applies different rate limits for GUEST accounts.

    Accounts whose :attr:`~validibot.users.models.User.user_kind` is
    ``GUEST`` get more restrictive rate limits than ``BASIC`` accounts.

    Usage:
        Set ``throttle_scope = "workflow_launch"`` on a view. This
        throttle will check for a ``guest_workflow_launch`` rate when
        the user is GUEST-classified, falling back to the normal scope
        rate when no guest-specific rate is configured.

    Settings example::

        REST_FRAMEWORK = {
            "DEFAULT_THROTTLE_RATES": {
                "workflow_launch": "60/minute",
                "guest_workflow_launch": "20/minute",
            }
        }

    Note: in community deployments (without the ``guest_management``
    Pro feature), every account is BASIC, so the guest-rate branch is
    inert. This is correct — without Pro guest invites, no account
    operates as a guest.
    """

    def get_cache_key(self, request, view):
        """Return a cache key, switching to a guest scope for GUEST kinds.

        For GUEST-classified accounts the scope is prefixed with
        ``guest_`` so a separate rate can apply. Anonymous users return
        ``None`` so DRF falls through to ``AnonRateThrottle``.
        """

        from validibot.users.constants import UserKindGroup

        if not request.user or not request.user.is_authenticated:
            return None  # Let AnonRateThrottle handle anonymous users

        if (
            getattr(request.user, "user_kind", UserKindGroup.BASIC)
            == UserKindGroup.GUEST
        ):
            guest_scope = f"guest_{self.scope}"
            if guest_scope in self.THROTTLE_RATES:
                self.scope = guest_scope
                self.rate = self.get_rate()
                self.num_requests, self.duration = self.parse_rate(self.rate)

        return super().get_cache_key(request, view)
