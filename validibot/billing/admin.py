"""
Django admin configuration for billing models.

Provides admin interfaces for:
- Plan: View/edit pricing plans and Stripe price IDs
- Subscription: View/manage organization subscriptions
- CreditPurchase: View credit purchase history
- UsageCounter: View usage metrics
"""

from django.contrib import admin

from validibot.billing.models import CreditPurchase
from validibot.billing.models import Plan
from validibot.billing.models import Subscription
from validibot.billing.models import UsageCounter


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    """Admin for pricing plans."""

    list_display = [
        "code",
        "name",
        "monthly_price_dollars",
        "basic_launches_limit",
        "included_credits",
        "max_seats",
        "stripe_price_id",
        "display_order",
    ]
    list_editable = ["stripe_price_id", "display_order"]
    ordering = ["display_order"]
    search_fields = ["code", "name"]

    fieldsets = [
        (None, {"fields": ["code", "name", "description"]}),
        (
            "Limits",
            {
                "fields": [
                    "basic_launches_limit",
                    "included_credits",
                    "max_workflows",
                    "max_custom_validators",
                    "max_seats",
                    "max_payload_mb",
                ],
            },
        ),
        ("Features", {"fields": ["has_integrations", "has_audit_logs"]}),
        (
            "Pricing & Stripe",
            {
                "fields": ["monthly_price_cents", "stripe_price_id"],
                "description": "Set stripe_price_id to the Stripe Price ID (price_xxx) for checkout.",
            },
        ),
        ("Display", {"fields": ["display_order"]}),
    ]


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    """Admin for organization subscriptions."""

    list_display = [
        "org",
        "plan",
        "status",
        "intended_plan",
        "trial_ends_at",
        "stripe_subscription_id",
    ]
    list_filter = ["status", "plan"]
    search_fields = ["org__name", "stripe_customer_id", "stripe_subscription_id"]
    raw_id_fields = ["org"]
    readonly_fields = ["created", "modified"]

    fieldsets = [
        (None, {"fields": ["org", "plan", "status"]}),
        (
            "Signup Intent",
            {
                "fields": ["intended_plan"],
                "description": "Plan selected from pricing page during signup.",
            },
        ),
        ("Trial", {"fields": ["trial_started_at", "trial_ends_at"]}),
        (
            "Stripe",
            {"fields": ["stripe_customer_id", "stripe_subscription_id"]},
        ),
        (
            "Credits",
            {"fields": ["included_credits_remaining", "purchased_credits_balance"]},
        ),
        (
            "Billing Period",
            {"fields": ["current_period_start", "current_period_end"]},
        ),
        (
            "Enterprise Overrides",
            {
                "fields": [
                    "custom_basic_launches_limit",
                    "custom_included_credits",
                    "custom_max_seats",
                ],
                "classes": ["collapse"],
            },
        ),
        ("Timestamps", {"fields": ["created", "modified"]}),
    ]


@admin.register(CreditPurchase)
class CreditPurchaseAdmin(admin.ModelAdmin):
    """Admin for credit purchase history."""

    list_display = [
        "subscription",
        "credits",
        "amount_cents",
        "stripe_payment_intent_id",
        "created",
    ]
    list_filter = ["created"]
    search_fields = ["subscription__org__name", "stripe_payment_intent_id"]
    readonly_fields = ["created", "modified"]


@admin.register(UsageCounter)
class UsageCounterAdmin(admin.ModelAdmin):
    """Admin for usage tracking."""

    list_display = [
        "org",
        "date",
        "period_start",
        "basic_launches",
        "advanced_launches",
        "credits_consumed",
    ]
    list_filter = ["date", "period_start"]
    search_fields = ["org__name"]
    raw_id_fields = ["org"]
    readonly_fields = ["created", "modified"]
