from django.apps import AppConfig


class ActionsConfig(AppConfig):
    """Application configuration for the workflow actions catalogue."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "simplevalidations.actions"

    def ready(self):
        # Import forms so action form registrations execute during app setup.
        from . import forms  # noqa: F401
