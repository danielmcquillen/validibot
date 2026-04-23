"""App config for the advanced analytics package."""

from django.apps import AppConfig


class AnalyticsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "validibot.analytics"
    verbose_name = "Advanced Analytics"
