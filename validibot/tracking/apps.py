from django.apps import AppConfig


class TrackingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "validibot.tracking"

    def ready(self):
        # Register signal handlers for login/logout tracking.
        from . import signals  # noqa: F401
