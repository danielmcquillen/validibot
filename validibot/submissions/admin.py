from django.contrib import admin
from django.contrib.admin import register

from validibot.submissions.models import Submission


@register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "org",
        "project",
        "user",
        "size_bytes",
        "created",
    )
    search_fields = ("name", "user__email", "id")
    list_filter = ("created", "size_bytes")
