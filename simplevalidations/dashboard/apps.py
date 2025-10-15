from django.apps import AppConfig


class DashboardConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "simplevalidations.dashboard"

    def ready(self):
        # Ensure built-in widgets register with the dashboard registry.
        from simplevalidations.dashboard import widgets  # noqa: F401
