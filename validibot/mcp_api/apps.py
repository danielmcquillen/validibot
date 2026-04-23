"""Django app configuration for the community MCP helper API."""

from django.apps import AppConfig


class MCPAPIConfig(AppConfig):
    """Register ``validibot.mcp_api`` as a Django app.

    Needed so the app's test modules and URL namespace (``mcp``) are
    discovered by Django. No ``ready()`` hook required — the views,
    authentication class, and ref helpers are all plain imports.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "validibot.mcp_api"
    verbose_name = "Validibot MCP API"
