from django.contrib import admin

from validibot.core.models import SiteSettings
from validibot.core.models import SupportMessage


# Register your models here.
@admin.register(SupportMessage)
class SupportMessageAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "subject",
        "created",
    )
    list_filter = ("created", "user")
    search_fields = ("subject", "message", "user__username", "user__email")
    readonly_fields = ("created", "modified")
    ordering = ("-created",)


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = (
        "slug",
        "modified",
    )
    readonly_fields = (
        "slug",
        "created",
        "modified",
    )
    fieldsets = (
        (
            "Site Configuration",
            {
                "fields": (
                    "slug",
                    "data",
                    "created",
                    "modified",
                ),
                "description": (
                    "Only system administrators should edit these values. "
                    "Keep JSON well-formed and rely on the application to fill "
                    "missing defaults."
                ),
            },
        ),
    )
