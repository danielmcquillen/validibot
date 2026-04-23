# Validibot MCP Server — Production Docker Compose image.
#
# Standalone FastMCP server with no Django dependency. Communicates with
# the community Validibot REST API at VALIDIBOT_API_BASE_URL.
#
# This is the community/self-hosted equivalent of validibot-cloud's
# deploy/Dockerfile.mcp. The two files drift independently: the cloud
# image is optimised for Cloud Run and mounts GCP Secret Manager
# secrets; this image is vanilla and reads a docker-compose env file.
#
# Build context: ``mcp/`` (the sibling Python project at mcp/ in this repo).
# The docker-compose.production.yml file passes this Dockerfile with the
# mcp/ directory as context, so COPY paths are relative to mcp/.
#
# Required env vars at runtime (see .envs.example/.production/.docker-compose/.mcp):
#   VALIDIBOT_API_BASE_URL  — URL of the Validibot Django API
#   VALIDIBOT_MCP_BASE_URL  — public URL of this MCP server
#   VALIDIBOT_OAUTH_AUTHORIZATION_SERVER_URL — upstream OAuth issuer
#   VALIDIBOT_MCP_ENABLED   — global kill switch (default true)
#   VALIDIBOT_MCP_SERVICE_KEY — shared secret for MCP→Django service auth

FROM python:3.13-slim AS base

WORKDIR /app

# uv for fast dependency resolution (matches the cloud image).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy the full package and install. The package is small; no layer
# caching win from splitting pyproject.toml and src/.
COPY pyproject.toml .
COPY src/ src/
RUN uv pip install --system --no-cache-dir .

ENV PORT=8080

# Uvicorn serves the ASGI app. The startup lifespan runs the license
# gate before accepting traffic.
CMD ["uvicorn", "validibot_mcp.server:app", "--host", "0.0.0.0", "--port", "8080"]
