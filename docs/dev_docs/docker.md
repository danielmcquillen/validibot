# Running Validibot with Docker

This project uses Docker Compose for local development. All services run in containers,
matching the Cookiecutter Django patterns with Celery + Redis for background task processing.

## Compose Files

Validibot has separate compose files for different environments. If you call Docker Compose directly, **you must specify which file to use**. For day-to-day use, prefer the `just` commands shown below.

| File                            | Purpose                                    | Command                                              |
| ------------------------------- | ------------------------------------------ | ---------------------------------------------------- |
| `docker-compose.local.yml`      | Local development (hot reload, runserver)  | `just local up`                                      |
| `docker-compose.production.yml` | Production-style (gunicorn, no code mount) | `just docker-compose bootstrap`                      |

There is no default `docker-compose.yml` — running `docker compose up` without `-f` will fail.

## Quick start (local development)

1. Create your local env files from the templates:

   ```bash
   mkdir -p .envs/.local
   cp .envs.example/.local/.django .envs/.local/.django
   cp .envs.example/.local/.postgres .envs/.local/.postgres
   # Optional for Pro/Enterprise
   cp .envs.example/.local/.build .envs/.local/.build
   ```

2. Edit the files and replace `!!!SET...!!!` placeholders with your values.

   > ⚠️ **Important**: The `.envs/` folder contains your actual secrets and is gitignored. Never commit it to version control, especially public repositories. See [Environment Configuration](deployment/environment-configuration.md) for details.

3. Build and start:

   ```bash
   docker compose -f docker-compose.local.yml up --build
   ```

   Or use the just command:

   ```bash
   just local up
   ```

   On first run, the web container automatically:
   - Applies database migrations
   - Runs `setup_validibot` to configure site settings, roles, validators, etc.

4. (Optional) Verify setup is correct:

   ```bash
   docker compose -f docker-compose.local.yml exec web python manage.py check_validibot
   ```

5. (Optional) Create a superuser if you didn't set `SUPERUSER_USERNAME` in `.envs/.local/.django`:

   ```bash
   docker compose -f docker-compose.local.yml exec web python manage.py createsuperuser
   ```

6. Visit http://localhost:8000

## `local-cloud` troubleshooting note

Most community users only need the standard `just local up` stack described above.
If you see `just local-cloud ...` elsewhere in the repo, that belongs to the
separate `validibot-cloud` development workflow rather than the normal
self-hosted community path.

If that `local-cloud` stack fails during startup with a `psycopg_c` error, the
most common cause is a stale shared virtualenv volume. `validibot-cloud` keeps
one `.venv` volume shared across containers, and `psycopg[c]` includes a
compiled extension. After dependency changes or base-image changes, that
compiled package can drift out of sync with the rest of the persisted
environment.

Reset the shared virtualenv volume and rebuild the stack:

```bash
docker compose -f ../validibot-cloud/docker-compose.cloud.yml down --remove-orphans
docker volume rm validibot_validibot_local_venv
just local-cloud up --build
```

## What's running

- `web`: Django app from `compose/local/django/Dockerfile`, mounted with your local code for hot reload (`runserver` on 8000). Runs migrations and initial setup on startup.
- `worker`: Celery worker processing background tasks from Redis queue. Spawns validator containers via Docker socket.
- `scheduler`: Celery Beat scheduler triggering periodic tasks (purge expired data, cleanup sessions, etc.).
- `postgres`: Postgres built from `compose/production/postgres/Dockerfile`, env vars from `.envs/.local/.postgres`.
- `redis`: Redis broker for Celery task queue.
- `mailpit`: Local SMTP capture at http://localhost:8025.

Entrypoint and start scripts live in `compose/local/django/` and wait for Postgres before launching.

## Production-style compose (for parity/testing)

Use `docker-compose.production.yml` to test production-like behavior locally. This runs Gunicorn instead of the Django dev server and doesn't mount your local code.

1. Create production env files from the templates:

   ```bash
   mkdir -p .envs/.production/.docker-compose
   cp .envs.example/.production/.docker-compose/.django .envs/.production/.docker-compose/.django
   cp .envs.example/.production/.docker-compose/.postgres .envs/.production/.docker-compose/.postgres
   # Optional for Pro/Enterprise
   cp .envs.example/.production/.docker-compose/.build .envs/.production/.docker-compose/.build
   ```

2. Edit the files with production-appropriate values:
   - Generate a proper `DJANGO_SECRET_KEY`
   - Set a strong `POSTGRES_PASSWORD`
   - Set `SUPERUSER_PASSWORD` and `SUPERUSER_EMAIL`

3. Validate the env files and bootstrap the stack:

   ```bash
   just docker-compose check-env
   just docker-compose bootstrap
   ```

   `bootstrap` builds and starts the production-style stack, applies migrations,
   runs `setup_validibot`, and finishes with `check_validibot`.

   The production `/start` script intentionally does **not** apply migrations on
   startup. That keeps the web process from racing schema changes and matches the
   expected self-host flow for customer deployments.

4. Visit http://localhost:8000

Production-style "web" serves user traffic via Gunicorn on port 8000. The "worker" processes background tasks via Celery. The "scheduler" runs Celery Beat for periodic tasks. This is the same stack used for [DigitalOcean deployments](deployment/digitalocean.md).

## VS Code: pytest "Run" button

VS Code's test runner needs environment variables. This repo includes a minimal env file at `.vscode/.env` that points tests at the Docker Postgres (using a `DATABASE_URL` with the local credentials from `docker-compose.local.yml`).

If the **Testing** panel hangs, double-check:

- VS Code is using the repo interpreter: `.venv/bin/python` (see `.vscode/settings.json`)
- Docker Compose is running: `docker compose -f docker-compose.local.yml up -d postgres`

## Advanced validators

The worker container spawns advanced validator containers (EnergyPlus, FMU, etc.) via the Docker socket. This requires:

1. **Docker socket mounted in the worker service** — Already configured in the production compose file
2. **Correct volume names** — The compose files assume `COMPOSE_PROJECT_NAME=validibot`
3. **Validator images available** — Must be pre-pulled or accessible from your registry

**Network isolation:** By default, advanced validator containers run with no network access (`network_mode='none'`). This is the most secure configuration — containers read/write via the shared storage volume and cannot reach other services or the internet. To enable network access (if validators need to download external files), uncomment `VALIDATOR_NETWORK` in the compose files.

For private registries, configure Docker credentials on the host before running validations. See [Execution Backends](overview/execution_backends.md) for details on registry authentication, network isolation, and naming requirements.

## Notes and deviations from full Cookiecutter setup

- **Celery + Redis** handles background tasks and scheduled jobs for Docker Compose deployments. For GCP, Cloud Tasks/Scheduler are used instead.
- Static/media: Whitenoise still works in-container, but long-term we'll move static/media to GCS + CDN as per the GCP ADR.
- Settings: the project already uses `django-environ`; `DATABASE_URL` is honored from the env files.
- Secrets: keep real secrets out of the repo; the examples are for local/dev only.
- Frontend assets: the Docker images do not run `npm install`/`npm run build`. If you change CSS/JS that relies on npm, build it locally (`npm install`, then `npm run build`) and ensure the generated assets are available to Django/Whitenoise (or your chosen static pipeline).

## Where things live

- `compose/local/django/Dockerfile`: base image for local dev (includes dev extras).
- `compose/production/django/Dockerfile`: base image for production (no dev extras).
- `docker-compose.local.yml`: local dev (runserver, code mounted) with web + worker + postgres + mailpit.
- `docker-compose.production.yml`: production-like (gunicorn, no code mount) with web + worker + postgres.
- `compose/local/django/entrypoint.sh` and `start.sh`: wait for DB, fix Docker socket permissions, run migrations, first-run setup, start dev server on 8000.
- `compose/production/django/entrypoint.sh` and `start.sh`: wait for DB, fix Docker socket permissions if mounted, collectstatic, skip setup until migrations exist, then start Gunicorn on 8000.
