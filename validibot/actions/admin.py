from django.contrib import admin

from validibot.actions import models


@admin.register(models.ActionDefinition)
class ActionDefinitionAdmin(admin.ModelAdmin):
    """Admin configuration for reusable action definitions."""

    list_display = ("name", "action_category", "type", "is_active")
    list_filter = ("action_category", "is_active")
    search_fields = ("name", "slug", "type")


@admin.register(models.Action)
class ActionAdmin(admin.ModelAdmin):
    """Admin configuration for workflow action instances."""

    list_display = ("name", "definition", "created", "modified")
    search_fields = ("name", "slug")
    list_filter = ("definition__action_category",)
