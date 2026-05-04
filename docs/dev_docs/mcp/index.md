# MCP Server

The Validibot MCP (Model Context Protocol) server is a standalone
FastMCP application that exposes validation workflows to AI agents
over the public MCP standard. This page is for contributors who want
to understand where the code lives, how to run it locally, and how it
gets deployed.

For end users connecting an AI assistant to a running deployment,
see the user-facing guide at
[docs.validibot.com/api/mcp-integration/](https://docs.validibot.com/api/mcp-integration/).

For the full deploy-side detail, see
[Deploy to GCP](../deployment/deploy-gcp.md).

## Where the code lives

| Concern | Path |
|---|---|
| FastMCP server source | `mcp/src/validibot_mcp/` |
| Tool implementations | `mcp/src/validibot_mcp/tools/` |
| Pydantic settings | `mcp/src/validibot_mcp/config.py` |
| Startup license check | `mcp/src/validibot_mcp/license_check.py` |
| Bearer-token + payment-signature auth | `mcp/src/validibot_mcp/auth.py` |
| Production Dockerfile | `compose/production/mcp/Dockerfile` |
| `just mcp` / `just gcp mcp` recipes | `just/mcp/mod.just` |
| Helper REST API the server proxies to | `validibot/mcp_api/` |
| OIDC provider that issues MCP OAuth tokens | `validibot/idp/` |

The MCP server is a separate Python project from the Django app — its
own `pyproject.toml`, its own dependency set (`fastmcp`, `httpx`,
`pydantic-settings`), and zero Django imports. It talks to the Django
REST API exactly the way the CLI does.

## The two-gate model

The MCP server is community code, but it only serves traffic on Pro+
deployments. Two independent gates protect it:

1. **Build-time:** `ENABLE_MCP_SERVER=true` (set in the stage's
   `.build` file) tells the deploy tooling to actually build and deploy
   the MCP container. When unset, every MCP-related `just` recipe
   short-circuits with a "skipped" message and exits 0.
2. **Runtime:** at startup the server calls
   `GET /api/v1/license/features/` against the Django API and refuses
   to serve traffic unless `mcp_server` is in the response. That feature
   is added by `validibot-pro`'s `License` declaration. Community-only
   deployments that build and start the container will see it exit
   immediately on this check.

Both gates exist deliberately: the build-time flag keeps the MCP
container out of stacks that don't need it; the runtime gate prevents
serving traffic from a build that somehow got through anyway.

## Two auth chains

Every MCP request involves two distinct authentication hops, and they
fail differently. Diagnostic output usually points to one or the other:

### Chain 1: end user → MCP server

The client (Claude Desktop, Cursor, etc.) authenticates the user via
OAuth 2.1. We support two paths:

- **OAuth with Dynamic Client Registration** (the modern path). The
  client POSTs to `/register`, then runs `/authorize` and `/token`
  through FastMCP's `OIDCProxy`, which forwards to Django's
  `validibot/idp/` endpoints. The user signs in normally; the client
  receives a JWT scoped to `validibot:mcp`.
- **Legacy bearer token.** The user creates an API token from their
  profile and the client sends it in the `Authorization: Bearer`
  header. Validated by `ValidibotTokenVerifier` in
  `mcp/src/validibot_mcp/token_verifier.py`.

### Chain 2: MCP server → Django REST API

When a tool needs to make a backend call, the MCP server calls
`validibot/mcp_api/` endpoints. That API requires its own service
identity proof. We support two paths:

- **Cloud Run OIDC identity tokens** (production). The MCP service
  account mints a Google-signed token with audience equal to the
  Django service URL. Django verifies the token + checks the SA is
  on `MCP_OIDC_ALLOWED_SERVICE_ACCOUNTS`.
- **Shared key** (`X-MCP-Service-Key`, local dev only). Sourced from
  Django's `MCP_SERVICE_KEY` setting and the MCP server's
  `VALIDIBOT_MCP_SERVICE_KEY` env var. Skip in production.

The end-user identity is forwarded separately as
`X-Validibot-User-Sub` (OIDC subject) or `X-Validibot-Api-Token`
(legacy), which `MCPUserRouteAuthentication` resolves to
`request.user`. The Django API never sees the MCP OAuth access token
directly — only the MCP server does.

## Running locally

Three flavors of local stack support MCP:

```bash
# community-only, no MCP — the container has no place to be in this stack
just local up

# community + Pro, with MCP container behind the "mcp" Compose profile
ENABLE_MCP_SERVER=true just local-pro up --build

# community + Pro + Cloud, same MCP container
ENABLE_MCP_SERVER=true just local-cloud up --build
```

`ENABLE_MCP_SERVER` can be set inline as above OR persisted in
`.envs/.local/.build` so you don't have to repeat it. With the flag
set, the MCP container listens on `http://localhost:8001`.

For tests:

```bash
just mcp test          # ruff + pytest, fully mocked, no GCP calls
just mcp test-e2e      # hits a live MCP server, requires .envs/.local/.test
```

## Deploying

On GCP:

```bash
source .envs/.production/.google-cloud/.just

# First-time only — provisions the MCP service account + IAM bindings
just gcp mcp setup prod

# Per-deploy — builds the image, pushes to Artifact Registry, deploys
# to Cloud Run. Driven by ENABLE_MCP_SERVER, VALIDIBOT_MCP_API_BASE_URL,
# and optional VALIDIBOT_X402_* public config in
# .envs/.production/.google-cloud/.build.
just gcp deploy-all prod    # web + worker + scheduler + MCP

# Or surgically:
just gcp mcp build
just gcp mcp deploy prod
```

For docker-compose self-hosters, MCP rides along when
`ENABLE_MCP_SERVER=true` is set in
`.envs/.production/.self-hosted/.build` — `just self-hosted up`
activates the `mcp` profile automatically.

## Where to look when something breaks

| Symptom | First place to look |
|---|---|
| Container exits at startup | `mcp/src/validibot_mcp/license_check.py` — license gate |
| `401 invalid_token` on tool call | Django audit/JWT verification + `mcp_api/authentication.py` |
| `Mismatching redirect URI` on OAuth | The allauth `Client` row's redirect URI vs. `VALIDIBOT_MCP_BASE_URL` in `.django` |
| `Connection issue — server config` | Client-cached failure; remove the connector and re-add fresh |
| 401 on `/api/v1/mcp/*` from MCP | `MCP_OIDC_AUDIENCE` + `MCP_OIDC_ALLOWED_SERVICE_ACCOUNTS` in `.django` |

For the full configuration matrix, see
[Environment Configuration → variable-to-file reference](../deployment/environment-configuration.md#variable-to-file-reference).
