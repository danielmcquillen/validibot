"""
Billing constants for the pricing system.

These enums define the plan codes and subscription lifecycle states used
throughout the billing module. PlanCode values serve as primary keys for
the Plan lookup table.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class PlanCode(models.TextChoices):
    """
    Plan codes used as primary key for Plan model.

    Three tiers: Starter (entry-level), Team (mid-tier), Enterprise (custom).
    No free tier - new orgs get a 2-week trial on Starter.
    """

    STARTER = "STARTER", _("Starter")
    TEAM = "TEAM", _("Team")
    ENTERPRISE = "ENTERPRISE", _("Enterprise")


class SubscriptionStatus(models.TextChoices):
    """
    Subscription lifecycle states.

    Typical flow for self-serve:
        TRIALING → ACTIVE (on payment)
        TRIALING → TRIAL_EXPIRED (if no payment after 14 days)
        ACTIVE → PAST_DUE (payment failed) → SUSPENDED (after grace period)
        ACTIVE → CANCELED (user cancels)

    Enterprise subscriptions are provisioned directly as ACTIVE.
    """

    TRIALING = "TRIALING", _("Trial")
    TRIAL_EXPIRED = "TRIAL_EXPIRED", _("Trial Expired")
    ACTIVE = "ACTIVE", _("Active")
    PAST_DUE = "PAST_DUE", _("Past Due")
    CANCELED = "CANCELED", _("Canceled")
    SUSPENDED = "SUSPENDED", _("Suspended")


# Trial duration in days
TRIAL_DURATION_DAYS = 14
