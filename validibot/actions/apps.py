from django.apps import AppConfig


class ActionsConfig(AppConfig):
    """Application configuration for the workflow actions catalogue."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "validibot.actions"

    def ready(self):
        # Register community action descriptors during app startup.
        # Commercial apps follow the same pattern from their own
        # AppConfig.ready() methods.
        from validibot.actions import forms  # noqa: F401
        from validibot.actions import handlers  # noqa: F401
        from validibot.actions.registrations import register_actions

        register_actions()
