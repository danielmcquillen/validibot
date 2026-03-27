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

The local stack uses `docker-compose.local.yml` and the root `just` commands:

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

# Edit .envs/.local/.django and set SUPERUSER_PASSWORD
just up
```

Once the containers are up:

- Open `http://localhost:8000`
- Sign in with the admin credentials from `.envs/.local/.django`
- Use `http://localhost:8025` to inspect locally captured emails

## If you purchased Pro or Enterprise

Local Docker builds can optionally bake a commercial package into the image. Do that before your first `just up`, or run `just build` afterwards to rebuild the stack.

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
Then add the commercial Django app to `config/settings/base.py` before you
start or rebuild the stack:

```python
INSTALLED_APPS += ["validibot_pro"]
```

If you purchased Enterprise, add both apps:

```python
INSTALLED_APPS += ["validibot_pro", "validibot_enterprise"]
```

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

That in-container path works for the standard `just up` stack. If you are
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
just build
just up
```

## Verify the install

Run these checks after the stack starts:

```bash
just ps
curl http://localhost:8000/health/
just manage "check_validibot"
```

If you want more detail while the app is starting, use:

```bash
just logs
```

## Common local commands

```bash
just up
just down
just build
just logs
just migrate
just manage "check_validibot"
just manage "createsuperuser"
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
specific to `validibot-cloud`, not the standard `just up` stack.

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
