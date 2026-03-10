from django.contrib import admin

from validibot.workflows.models import Workflow
from validibot.workflows.models import WorkflowStep
from validibot.workflows.models import WorkflowStepResource


@admin.register(Workflow)
class WorkflowAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "name",
        "org",
        "project",
        "user",
        "slug",
        "version",
        "is_active",
        "is_archived",
        "make_info_page_public",
        "is_public",
        "allowed_file_types",
        "created",
        "modified",
    )
    search_fields = (
        "name",
        "slug",
    )
    ordering = ("name",)


class WorkflowStepResourceInline(admin.TabularInline):
    model = WorkflowStepResource
    extra = 0
    fields = (
        "role",
        "validator_resource_file",
        "step_resource_file",
        "filename",
        "resource_type",
    )
    readonly_fields = ()


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
    list_filter = ("workflow",)
    search_fields = (
        "name",
        "workflow__name",
    )
    ordering = ("workflow", "order")
    inlines = [WorkflowStepResourceInline]
