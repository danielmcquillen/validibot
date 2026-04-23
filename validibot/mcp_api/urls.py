"""URL patterns for the community MCP helper API.

Mounted under ``/api/v1/mcp/`` by ``config.api_router``. The namespace
``mcp`` matches what ``views.MCPWorkflowRunCreateView.post()`` uses when
it builds the ``Location`` header via
``reverse("mcp:run-detail", ...)`` after creating a run.
"""

from django.urls import path

from validibot.mcp_api import views

app_name = "mcp"

urlpatterns = [
    path(
        "workflows/",
        views.MCPWorkflowCatalogView.as_view(),
        name="workflow-list",
    ),
    path(
        "workflows/<str:workflow_ref>/",
        views.MCPWorkflowDetailView.as_view(),
        name="workflow-detail",
    ),
    path(
        "workflows/<str:workflow_ref>/runs/",
        views.MCPWorkflowRunCreateView.as_view(),
        name="workflow-runs",
    ),
    path(
        "runs/<str:run_ref>/",
        views.MCPRunDetailView.as_view(),
        name="run-detail",
    ),
]
