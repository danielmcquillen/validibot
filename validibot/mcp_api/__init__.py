"""REST surface that the FastMCP server at ``validibot/mcp/`` calls.

These endpoints live in the community repo so self-hosted Pro operators
get a working MCP out of the box. Before this package existed, the MCP
server's ``client.py`` hit ``/api/v1/mcp/*`` routes that only the hosted
cloud stack exposed — a self-hosted deployment would pass the startup
license gate then 404 on every tool call.

The endpoints return cross-org authenticated workflow catalogs, workflow
detail, run creation, and run status — all keyed by opaque refs so the
MCP contract never exposes internal routing details (org slug,
workflow-version UUIDs) to agents.

Authentication uses the service-to-service pattern in
``validibot.mcp_api.authentication``: the MCP process authenticates
itself to Django using either a Cloud Run OIDC identity token
(production) or a shared secret (local dev), and the end user's
identity is forwarded as a separate header that Django resolves to a
``request.user``.

Cloud runs its own x402-paid surface at ``/api/v1/agent/*`` for
anonymous agents — that stays in ``validibot-cloud/validibot_cloud/agents/``
because it depends on cloud-only ``AgentValidationRun`` /
``X402Payment`` models.
"""
