"""Django app configuration for the Validibot OIDC provider."""

from django.apps import AppConfig


class ValidibotIDPConfig(AppConfig):
    """Register Validibot OIDC helpers (management commands, templates)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "validibot.idp"
    verbose_name = "Validibot OIDC"
