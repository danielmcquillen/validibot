# Running Validibot with Docker

This project uses Docker Compose for local development. All services run in containers,
matching the Cookiecutter Django patterns with Celery + Redis for background task processing.

## Compose Files

Validibot has separate compose files for different environments. **You must specify which file to use**:

| File                            | Purpose                                    | Command                                              |
| ------------------------------- | ------------------------------------------ | ---------------------------------------------------- |
| `docker-compose.local.yml`      | Local development (hot reload, runserver)  | `docker compose -f docker-compose.local.yml up`      |
| `docker-compose.production.yml` | Production-style (gunicorn, no code mount) | `docker compose -f docker-compose.production.yml up` |

There is no default `docker-compose.yml` — running `docker compose up` without `-f` will fail.

## Quick start (local development)

1. Create your local env files from the templates:

   ```bash
   mkdir -p .envs/.local
   cp .envs.example/.local/.django .envs/.local/.django
   cp .envs.example/.local/.postgres .envs/.local/.postgres
   ```

2. Edit the files and replace `!!!SET...!!!` placeholders with your values.

   > ⚠️ **Important**: The `.envs/` folder contains your actual secrets and is gitignored. Never commit it to version control, especially public repositories. See [Environment Configuration](deployment/environment-configuration.md) for details.

3. Build and start:

   ```bash
   docker compose -f docker-compose.local.yml up --build
   ```

   Or use the just command:

   ```bash
   just up
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
   ```

2. Edit the files with production-appropriate values:
   - Generate a proper `DJANGO_SECRET_KEY`
   - Set a strong `POSTGRES_PASSWORD`
   - Set `SUPERUSER_PASSWORD` and `SUPERUSER_EMAIL`

3. Build and run:

   ```bash
   docker compose -f docker-compose.production.yml up --build
   ```

   On first run, the web container automatically:
   - Applies database migrations
   - Collects static files
   - Runs `setup_validibot` to configure site settings, roles, validators, etc.
   - Creates a superuser if `SUPERUSER_USERNAME` is set in the env file

4. Visit http://localhost:8000

Production-style "web" serves user traffic via Gunicorn on port 8000. The "worker" processes background tasks via Celery. The "scheduler" runs Celery Beat for periodic tasks. This is the same stack used for [DigitalOcean deployments](deployment/digitalocean.md).

## VS Code: pytest "Run" button

VS Code's test runner needs environment variables. This repo includes a minimal env file at `.vscode/.env` that points tests at the Docker Postgres (using a `DATABASE_URL` with the local credentials from `docker-compose.local.yml`).

If the **Testing** panel hangs, double-check:

- VS Code is using the repo interpreter: `.venv/bin/python` (see `.vscode/settings.json`)
- Docker Compose is running: `docker compose -f docker-compose.local.yml up -d postgres`

## Advanced validators

The worker container spawns advanced validator containers (EnergyPlus, FMI, etc.) via the Docker socket. This requires:

1. **Docker socket mounted** — Already configured in the compose files
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
- `compose/production/django/entrypoint.sh` and `start.sh`: wait for DB, fix Docker socket permissions, run migrations, collectstatic, first-run setup, start Gunicorn on 8000.
