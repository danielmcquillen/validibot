from django.contrib import admin

from simplevalidations.marketing.models import Prospect


@admin.register(Prospect)
class ProspectAdmin(admin.ModelAdmin):
    list_display = ("email", "origin", "source", "welcome_sent_at", "created")
    list_filter = ("origin", "welcome_sent_at")
    search_fields = ("email", "source", "referer")
    readonly_fields = ("created", "modified", "referer", "user_agent", "ip_address")
