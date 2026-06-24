"""
Exceptions raised by the validation launch/run services.

These give callers a way to distinguish *why* a launch was refused so the
UI can show an accurate message. Historically the launch path raised a bare
``PermissionError`` for two very different situations:

1. The user genuinely lacks permission to run the workflow
   (``workflow.can_execute()`` is False).
2. The user *is* allowed, but an organization policy blocked the launch —
   billing isn't set up, the trial expired, a quota or rate limit was hit,
   or there aren't enough compute credits.

The views caught ``PermissionError`` and rendered a single hardcoded string,
"You do not have permission to run this workflow." For case 2 that message is
actively misleading: the user has permission, and the real reason (and how to
fix it) was thrown away.

``OrgPolicyDeniedError`` separates case 2 so the views can surface the policy's own
human-readable reason. It subclasses ``PermissionError`` deliberately: any
existing ``except PermissionError`` continues to catch it, so introducing this
type changes no behavior for callers that don't opt in — it only lets callers
that *do* care tell the two cases apart and show the better message.
"""

from __future__ import annotations


class OrgPolicyDeniedError(PermissionError):
    """A launch the user is permitted to make was blocked by an org policy.

    Carries the policy's human-readable reason (billing not set up, quota
    exceeded, insufficient credits, rate limited, ...) as its message, so the
    UI can show *why* the launch was refused and what to do about it, rather
    than a generic "no permission" string.

    Subclasses ``PermissionError`` so that code which only catches
    ``PermissionError`` still treats it as a refusal — this is purely additive.
    """
