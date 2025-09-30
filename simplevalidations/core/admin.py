from django.contrib import admin

from simplevalidations.core.models import SupportMessage


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
