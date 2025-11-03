from django.contrib import admin

from simplevalidations.workflows.models import WorkflowStep


@admin.register(WorkflowStep)
class WorkflowStepAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "workflow",
        "name",
        "order",
        "created",
        "modified",
    )
    list_filter = (
        "workflow",
    )
    search_fields = (
        "name",
        "workflow__name",
    )
    ordering = ("workflow", "order")
