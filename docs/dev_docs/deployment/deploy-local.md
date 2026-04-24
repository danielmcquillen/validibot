# Run Validibot Locally

This is the fastest way to try Validibot on your own machine. It is the right place to start if you just bought a self-hosting license, want to evaluate the product locally, or need a private sandbox before moving to a server.

Most first-time users should start here.

## When to choose this target

Choose the local target if you want:

- A quick single-machine install for evaluation or development
- The shortest path from clone to running app
- A safe place to learn the product before exposing it on a network

Choose [Deploy with Docker Compose](deploy-docker-compose.md) instead if you want a long-lived server or a deployment that other people can access over the network.

## What this target runs

The local stack uses `docker-compose.local.yml` and the `just local` commands:

- `web` running Django with local code mounted in
- `worker` for background jobs
- `scheduler` for periodic tasks
- `postgres` for the database
- `redis` for the task queue
- `mailpit` for local email capture

On first start, the local web container applies migrations and runs `setup_validibot` automatically.

## Prerequisites

Before you start, make sure you have:

- Docker Desktop or Docker Engine installed
- [just](https://just.systems/) installed
- At least 4 GB of RAM available to Docker

## Quick start

```bash
git clone https://github.com/danielmcquillen/validibot.git
cd validibot

mkdir -p .envs/.local
cp .envs.example/.local/.django .envs/.local/.django
cp .envs.example/.local/.postgres .envs/.local/.postgres

# Also copy the build/recipe config. It holds both build-time knobs
# (commercial-package installation) and recipe-level knobs
# (ENABLE_MCP_SERVER for the Pro stacks). Safe to copy even for
# community-only use — every variable has a sensible default.
cp .envs.example/.local/.build .envs/.local/.build

# Edit .envs/.local/.django and set SUPERUSER_PASSWORD
just local up
```

Once the containers are up:

- Open `http://localhost:8000`
- Sign in with the admin credentials from `.envs/.local/.django`
- Use `http://localhost:8025` to inspect locally captured emails

## If you purchased Pro or Enterprise

Local Docker builds can optionally bake a commercial package into the image. Do that before your first `just local up`, or run `just local build` afterwards to rebuild the stack.

If you already copied `.envs.example/.local/.build` in the Quick start above, just edit `.envs/.local/.build`. Otherwise:

```bash
cp .envs.example/.local/.build .envs/.local/.build
```

Then edit `.envs/.local/.build` and set:

```bash
VALIDIBOT_COMMERCIAL_PACKAGE=validibot-pro==<version>
VALIDIBOT_PRIVATE_INDEX_URL=https://<license-credentials>@pypi.validibot.com/simple/
```

Use `validibot-enterprise==<version>` instead of `validibot-pro==<version>` if
you purchased Enterprise. You can also use a quoted exact wheel URL on
`pypi.validibot.com` that includes `#sha256=<hash>` instead of a package name
and version.

Then point Django at the Pro-activating settings module by setting
`DJANGO_SETTINGS_MODULE` in `.envs/.local/.django`:

```bash
DJANGO_SETTINGS_MODULE=config.settings.local_pro
```

That's all — the settings module adds `validibot_pro` to
`INSTALLED_APPS`, which is what Django needs in order to import the
package and run its license-registration hook. Do not patch
`config/settings/base.py` directly (that makes upgrades messier);
the dedicated settings module is the supported path.

For Enterprise, use the same pattern with a forthcoming
`config.settings.local_enterprise` module (or stack two modules via
`DJANGO_SETTINGS_MODULE=config.settings.local_enterprise` which will
append both `validibot_pro` and `validibot_enterprise`).

## Include the MCP server

The standalone FastMCP container exposes validation workflows to AI
agents (Claude, Cursor, etc.) over the Model Context Protocol. The
code itself lives in this repo at `mcp/` — it's community, not Pro
— so self-hosted deployments can build and run it. At runtime,
though, the container calls `GET /api/v1/license/features/` against
the Django API and refuses to serve traffic unless `mcp_server` is
advertised, which only happens when `validibot-pro` (or enterprise)
is installed. So the practical picture is:

- **Community-only stack**: the container can be built, but it will
  fail its license check on startup and exit. Useful for
  contributors hacking on the MCP code, not for end users.
- **Pro/Cloud stack**: the license check passes, MCP serves requests
  normally.

The container is defined in the `local-pro` and `local-cloud`
compose overlays behind an opt-in `mcp` Compose profile, so an
empty `.build` file leaves it out by default.

To include it, open `.envs/.local/.build` and set:

```bash
ENABLE_MCP_SERVER=true
```

Then restart the stack:

```bash
just local-pro up --build    # community + Pro + MCP
# or
just local-cloud up --build  # community + Pro + Cloud + MCP
```

The recipe prints `"ENABLE_MCP_SERVER is set — including the MCP container (profile: mcp)"` on start, and the container listens on `http://localhost:8001`.

`ENABLE_MCP_SERVER=true` is ignored by `just local up` because the community local compose file defines no `mcp` service — if you want to exercise MCP locally, use `local-pro` or `local-cloud`.

## Enable signed credentials locally

If you want to test the signed credential action locally, generate a small
local signing key and point `SIGNING_KEY_PATH` at it.

Create the key on the host:

```bash
mkdir -p .envs/.local/keys
openssl ecparam -name prime256v1 -genkey -noout \
  -out .envs/.local/keys/credential-signing.pem
chmod 600 .envs/.local/keys/credential-signing.pem
```

Then add this to `.envs/.local/.django`:

```bash
SIGNING_KEY_PATH=/run/validibot-keys/credential-signing.pem
CREDENTIAL_ISSUER_URL=http://localhost:8000
```

That in-container path works for the standard `just local up` stack. If you are
using the separate `just local-cloud ...` development flow, use this instead:

```bash
SIGNING_KEY_PATH=/app/.envs/.local/keys/credential-signing.pem
CREDENTIAL_ISSUER_URL=http://localhost:8000
```

`local-cloud` mounts the full `validibot` repo at `/app`, so the key is
available there even though that stack does not currently mount
`/run/validibot-keys`.

After updating the env file, rebuild or restart the stack:

```bash
just local build
just local up
```

## Verify the install

Run these checks after the stack starts:

```bash
just local ps
curl http://localhost:8000/health/
just local manage "check_validibot"
```

If you want more detail while the app is starting, use:

```bash
just local logs
```

## Common local commands

```bash
just local up
just local down
just local build
just local logs
just local migrate
just local manage "check_validibot"
just local manage "createsuperuser"
```

See [Justfile Guide](justfile-guide.md) for the full command reference.

## Advanced validators locally

Built-in validators work as soon as the local stack is running. Advanced validators such as EnergyPlus and FMU run as sibling containers launched by the worker, so you also need the relevant validator images available on the Docker host.

For consistency with the production stack, only the local `worker` service gets Docker socket access. The `web` and `scheduler` containers do not need it.

If you plan to test advanced validators locally, check:

- [Docker Setup](../docker.md)
- [Execution Backends](../overview/execution_backends.md)

## Important note about `local-cloud`

If you see `just local-cloud ...` elsewhere in the repo, that is for the separate `validibot-cloud` development workflow. It is not the standard self-host path for a customer running Validibot locally.

If you hit a startup error in that separate `local-cloud` flow mentioning
`psycopg_c`, the usual cause is a stale shared virtualenv volume. That issue is
specific to `validibot-cloud`, not the standard `just local up` stack.

`local-cloud` keeps a shared `.venv` volume for its web and worker containers.
`psycopg[c]` includes a compiled extension, so after dependency changes or base
image changes the compiled package can get out of sync with the rest of that
persisted environment.

Reset the shared virtualenv volume and rebuild:

```bash
docker compose -f ../validibot-cloud/docker-compose.cloud.yml down --remove-orphans
docker volume rm validibot_validibot_local_venv
just local-cloud up --build
```

## Where to go next

Once you are comfortable running locally:

- Move to [Deploy with Docker Compose](deploy-docker-compose.md) for a single-host production deployment
- Move to [Deploy to GCP](deploy-gcp.md) if you want a managed cloud deployment
- Read [Environment Configuration](environment-configuration.md) for the env file structure
