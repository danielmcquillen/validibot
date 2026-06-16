from django.apps import AppConfig


class TrackingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "validibot.tracking"

    def ready(self):
        # Register signal handlers for login/logout + signup/email-verified
        # tracking — these self-connect via @receiver on import.
        from . import signals

        # Model-lifecycle tracking (submission / workflow / ruleset /
        # validator) is wired explicitly so other apps' models aren't
        # imported until the app registry is ready.
        signals.connect_model_tracking_receivers()
