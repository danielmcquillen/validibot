"""
Validibot MCP Server — exposes validation workflows to AI agents.

This package is a standalone FastMCP server with zero Django or
validibot-cloud dependencies. It communicates with Validibot
exclusively through the REST API (same pattern as the CLI), which
keeps the container small and the deployment boundary clean.

Placement and licensing
-----------------------

The MCP server lives inside the community ``validibot`` repository
so its implementation is auditable and reusable, but it is gated
behind the ``mcp_server`` commercial feature flag. At startup the
server calls ``GET /api/v1/license/features/`` against its configured
Validibot API and refuses to boot unless ``mcp_server`` is advertised
— that is, unless ``validibot-pro`` (or ``validibot-enterprise``) is
installed on the Validibot deployment it fronts. See
``license_check.py`` for the gate implementation.

Packaging-wise this is a sibling project to the Django application
that shares the repository: it has its own ``pyproject.toml`` and
dependency set (``fastmcp``, ``httpx``, ``pydantic-settings``) with
no Django imports.

Deployment
----------

The production image for validibot.com lives in the private
``validibot-cloud`` repository (``deploy/Dockerfile.mcp``). It copies
this directory into the build context and installs the wheel.
Self-hosted Pro users can follow the same pattern to run their own
MCP endpoint.

x402 / agent payments
---------------------

Code implementing the x402 payment protocol is still bundled in this
package (``x402.py``, branches inside ``tools/validate.py`` and
``tools/runs.py``). It is dormant unless the x402 environment variables
are configured — in practice that only happens in the hosted cloud
deployment, which also operates the receiving wallet and owns the
agent-run database models in ``validibot-cloud``. Carving the x402
bits into a separate cloud-side overlay package is tracked as follow-up
work; the code itself is an implementation of a public Linux Foundation
spec, so hosting it in community carries no licensing exposure.
"""
