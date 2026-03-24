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
VALIDIBOT_COMMERCIAL_PACKAGE=validibot-pro
VALIDIBOT_PRIVATE_INDEX_URL=https://<license-credentials>@pypi.validibot.com/simple/
```

Use `validibot-enterprise` instead of `validibot-pro` if you purchased Enterprise.
You do not need to edit `config/settings/base.py`; the core app discovers installed commercial packages automatically.

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

If you plan to test advanced validators locally, check:

- [Docker Setup](../docker.md)
- [Execution Backends](../overview/execution_backends.md)

## Important note about `local-cloud`

If you see `just local-cloud ...` elsewhere in the repo, that is for the separate `validibot-cloud` development workflow. It is not the standard self-host path for a customer running Validibot locally.

## Where to go next

Once you are comfortable running locally:

- Move to [Deploy with Docker Compose](deploy-docker-compose.md) for a single-host production deployment
- Move to [Deploy to GCP](deploy-gcp.md) if you want a managed cloud deployment
- Read [Environment Configuration](environment-configuration.md) for the env file structure
