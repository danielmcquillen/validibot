from django.apps import AppConfig


class BillingConfig(AppConfig):
    """
    Django app configuration for the billing app.

    Handles Stripe billing integration, subscription management,
    and usage metering.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "validibot.billing"

    def ready(self):
        """
        Import webhook handlers to register signal receivers.

        dj-stripe 2.9+ uses Django signals (webhook_received) instead of
        decorators. We import the webhooks module to register our signal
        receivers when Django starts.
        """
        from validibot.billing import webhooks  # noqa: F401
