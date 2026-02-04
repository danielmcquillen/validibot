from django.apps import AppConfig


class DashboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "validibot.dashboard"

    def ready(self):
        # Ensure built-in widgets register with the dashboard registry.
        from validibot.dashboard import widgets  # noqa: F401
