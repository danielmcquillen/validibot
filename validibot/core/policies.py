"""
Organization policy registry for Validibot.

This module provides a thin hook for enforcing organization-level policies.
Policies are action-specific checks that determine whether an organization is
allowed to perform a given action (e.g., launching a validation run).

The community edition ships with no policies registered, so all actions are
allowed by default. Commercial packages (validibot-cloud, validibot-pro,
validibot-enterprise) register their own policy functions at app-ready time
to enforce business rules like trial expiry, usage quotas, or billing status.

This follows the same registry pattern as ``core/license.py`` and
``core/features.py``: core provides the hook, commercial packages provide
the logic.

Usage in core enforcement points::

    from validibot.core.policies import check_org_policies

    allowed, reason = check_org_policies(org, "launch_validation_run")
    if not allowed:
        raise PermissionError(reason)
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from validibot.users.models import Organization

logger = logging.getLogger(__name__)

# Type alias for policy functions.
# A policy receives an Organization and an action string, and returns a
# tuple of (allowed: bool, reason: str). If allowed is False, reason
# should explain why the action was denied.
OrgPolicyFn = Callable[["Organization", str], tuple[bool, str]]

# Internal registry — commercial packages register policy functions here.
# Multiple policies can be registered; they are checked in registration order.
_org_policies: list[OrgPolicyFn] = []


def register_org_policy(policy_fn: OrgPolicyFn) -> None:
    """
    Register an organization policy function.

    Called by commercial packages at app-ready time to add policy checks.
    Multiple policies can be registered; they are all checked in order.

    Args:
        policy_fn: A callable that takes (org, action) and returns
            (allowed, reason). If allowed is False, reason should
            explain why the action was denied.
    """
    _org_policies.append(policy_fn)
    logger.info(
        "Org policy registered: %s.%s",
        policy_fn.__module__,
        policy_fn.__qualname__,
    )


def check_org_policies(org: Organization, action: str) -> tuple[bool, str]:
    """
    Run all registered organization policies for the given action.

    Policies are checked in registration order. The first policy that
    denies the action wins — its reason is returned immediately.

    If no policies are registered (community edition), the action is
    always allowed.

    Args:
        org: The organization attempting the action.
        action: A string identifying the action (e.g., "launch_validation_run").

    Returns:
        A tuple of (allowed, reason). If allowed is True, reason is empty.
        If allowed is False, reason explains why.
    """
    for policy_fn in _org_policies:
        allowed, reason = policy_fn(org, action)
        if not allowed:
            logger.info(
                "Org policy denied: org=%s action=%s reason=%s policy=%s.%s",
                org,
                action,
                reason,
                policy_fn.__module__,
                policy_fn.__qualname__,
            )
            return (False, reason)
    return (True, "")


def reset_org_policies() -> None:
    """
    Clear all registered organization policies (for testing).
    """
    _org_policies.clear()
