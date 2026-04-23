"""
Workflow discovery tools: list_workflows and get_workflow_details.

These tools let agents browse available validation workflows and inspect
their configuration before submitting files.

The ``get_workflow_details`` tool enriches the raw API response with
computed fields that help agents make informed decisions:

- ``accepted_extensions``: Concrete file extensions the workflow accepts
  (e.g. ``.json``, ``.idf``) — agents need this to know what files to submit.
- ``pricing``: Structured pricing summary — agents need this to know
  whether they'll be charged and how much.
- ``validation_summary``: Human-readable description of the workflow's
  steps — gives agents a quick overview without parsing the full steps array.

This enrichment follows Anthropic's guidance on writing tools for agents:
"return semantic fields" and "prioritize signal over flexibility". Agents
can always dig into the raw ``steps`` array for full details.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from validibot_mcp import auth, client
from validibot_mcp.errors import MCPToolError
from validibot_mcp.gating import check_agent_access, check_global_enabled
from validibot_mcp.refs import build_workflow_ref
from validibot_mcp.tools import format_error

if TYPE_CHECKING:
    from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# File type → extension mapping
#
# The Workflow model stores logical file types (JSON, XML, TEXT, etc.).
# Agents need to know the concrete file extensions they can submit.
# This map translates between the two.
# ---------------------------------------------------------------------------
_FILE_TYPE_EXTENSIONS: dict[str, list[str]] = {
    "json": [".json", ".epjson"],
    "xml": [".xml", ".thmx"],
    "text": [".idf", ".txt", ".csv", ".yaml", ".yml"],
    "yaml": [".yaml", ".yml"],
    "binary": [".fmu", ".thmz", ".zip"],
}

_READ_ONLY_TOOL_ANNOTATIONS = {
    "readOnlyHint": True,
    "idempotentHint": True,
    "openWorldHint": True,
}


def _build_accepted_extensions(allowed_file_types: list[str]) -> list[str]:
    """Map logical file types to concrete file extensions.

    Args:
        allowed_file_types: List of file type codes from the workflow
            (e.g. ["json", "text"]).

    Returns:
        Deduplicated, sorted list of file extensions (e.g. [".csv", ".idf",
        ".json", ".txt", ".yaml", ".yml"]).
    """
    extensions: set[str] = set()
    for ft in allowed_file_types:
        extensions.update(_FILE_TYPE_EXTENSIONS.get(ft.lower(), []))
    return sorted(extensions)


def _build_pricing(workflow: dict[str, Any]) -> dict[str, Any]:
    """Build a structured pricing summary for agents.

    Translates the raw ``agent_billing_mode`` and ``agent_price_cents``
    fields into a human-readable pricing object.
    """
    mode = workflow.get("agent_billing_mode", "AUTHOR_PAYS")
    price_cents = workflow.get("agent_price_cents", 0) or 0

    if mode == "AGENT_PAYS_X402":
        return {
            "mode": "AGENT_PAYS_X402",
            "price_cents": price_cents,
            "price_display": f"${price_cents / 100:.2f} USD",
            "currency": "usd",
            "payment_required": True,
        }
    return {
        "mode": "AUTHOR_PAYS",
        "payment_required": False,
    }


def _build_validation_summary(workflow: dict[str, Any]) -> str:
    """Build a human-readable summary of what the workflow validates.

    Agents use this for quick decision-making before inspecting the full
    ``steps`` array. The summary lists each step with its name and
    validator type (or action type for non-validation steps).
    """
    steps = workflow.get("steps", [])
    if not steps:
        return "This workflow has no validation steps configured."

    parts: list[str] = []
    for i, step in enumerate(steps, 1):
        name = step.get("name") or step.get("description") or "Unnamed step"
        validator = step.get("validator")
        if validator:
            vtype = validator.get("name") or validator.get("validation_type", "")
            parts.append(f"({i}) {name} — {vtype}")
        else:
            action = step.get("action_type") or "action"
            parts.append(f"({i}) {name} — {action}")

    step_word = "step" if len(steps) == 1 else "steps"
    return f"This workflow has {len(steps)} validation {step_word}: " + ", ".join(parts) + "."


def _enrich_workflow_for_agent(workflow: dict[str, Any]) -> dict[str, Any]:
    """Add computed fields to a workflow response for agent consumption.

    This function is applied to the ``get_workflow_details`` response before
    returning it to the agent. It adds three computed fields:

    - ``accepted_extensions``: Concrete file extensions the workflow accepts.
    - ``pricing``: Structured pricing summary.
    - ``validation_summary``: Human-readable description of the workflow steps.

    The original workflow dict is not mutated — a new dict is returned.
    """
    enriched = _with_workflow_ref(workflow)
    enriched["accepted_extensions"] = _build_accepted_extensions(
        workflow.get("allowed_file_types", []),
    )
    enriched["pricing"] = _build_pricing(workflow)
    enriched["validation_summary"] = _build_validation_summary(workflow)
    return enriched


def _with_workflow_ref(workflow: dict[str, Any]) -> dict[str, Any]:
    """Attach ``workflow_ref`` when enough routing metadata is available."""

    enriched = {**workflow}
    if enriched.get("workflow_ref"):
        return enriched

    org_slug = str(enriched.get("org_slug") or enriched.get("org") or "").strip()
    workflow_slug = str(enriched.get("slug") or "").strip()
    if org_slug and workflow_slug:
        enriched["workflow_ref"] = build_workflow_ref(
            org_slug=org_slug,
            workflow_slug=workflow_slug,
        )
    return enriched


def _invalid_params(message: str) -> dict[str, Any]:
    """Return the standard MCP tool error shape for invalid inputs."""

    return {
        "error": {
            "code": "INVALID_PARAMS",
            "message": message,
        },
    }


async def list_workflows() -> list[dict[str, Any]] | dict[str, Any]:
    """List validation workflows available for agent access.

    Two modes depending on whether you have a Validibot bearer credential:

    **Authenticated** (with OAuth or a manual bearer token): returns all
    MCP-accessible workflows available to you across every organization you
    belong to, plus all public x402 workflows.

    **Anonymous** (no API key): browse all workflows published for anonymous
    agent access across all organizations.
    Only x402-payable workflows are returned.

    Returns:
        Array of workflow summaries with slug, name, version,
        allowed_file_types, agent_access_enabled, and agent_price_cents.
    """
    try:
        check_global_enabled()
        api_key = auth.get_api_key_or_none()
        user_sub = auth.get_authenticated_user_sub_or_none()

        if api_key is not None:
            return await client.list_authenticated_workflows(
                user_sub=user_sub,
                api_token=None if user_sub else api_key,
            )

        # Anonymous path — hit the cross-org agent discovery endpoint.
        workflows = await client.list_agent_workflows()
        return [_with_workflow_ref(workflow) for workflow in workflows]

    except MCPToolError as exc:
        return format_error(exc)


async def get_workflow_details(
    workflow_ref: str = "",
) -> dict[str, Any]:
    """Get full details of a validation workflow including its steps.

    Use ``workflow_ref`` from ``list_workflows``. The tool keeps ``org_slug`` in
    discovery results for display and disambiguation, but the MCP contract
    routes subsequent calls through this opaque handle.
    """
    try:
        check_global_enabled()
        if not workflow_ref:
            return _invalid_params("workflow_ref is required.")

        api_key = auth.get_api_key_or_none()
        user_sub = auth.get_authenticated_user_sub_or_none()

        if api_key is not None:
            workflow = await client.get_authenticated_workflow_detail(
                workflow_ref,
                user_sub=user_sub,
                api_token=None if user_sub else api_key,
            )
        else:
            workflow = await client.get_agent_workflow_detail(workflow_ref)

        access_modes = workflow.get(
            "access_modes",
            ["member_access"] if api_key is not None else ["public_x402"],
        )
        if api_key is None or "member_access" not in access_modes:
            check_agent_access(workflow)
        return _enrich_workflow_for_agent(workflow)
    except MCPToolError as exc:
        return format_error(exc)


def register_tools(server: FastMCP) -> None:
    """Register the workflow tools on a FastMCP server instance."""

    server.tool(list_workflows, annotations=_READ_ONLY_TOOL_ANNOTATIONS)
    server.tool(get_workflow_details, annotations=_READ_ONLY_TOOL_ANNOTATIONS)
