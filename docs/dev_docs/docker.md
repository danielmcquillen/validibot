# Running Validibot Locally with Docker

This project uses Docker Compose for local development. All services run in containers,
matching the Cookiecutter Django patterns with Celery + Redis for background task processing.

## Compose Files

Validibot has separate compose files for different environments. **You must specify which file to use**:

| File | Purpose | Command |
|------|---------|---------|
| `docker-compose.local.yml` | Local development (hot reload, runserver) | `docker compose -f docker-compose.local.yml up` |
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

4. Apply migrations (the start script does this, but you can rerun):
   ```bash
   docker compose -f docker-compose.local.yml exec django uv run python manage.py migrate
   ```

5. Run initial setup (first time only):
   ```bash
   docker compose -f docker-compose.local.yml exec django uv run python manage.py setup_validibot --domain localhost:8000
   ```

6. Verify setup is correct:
   ```bash
   docker compose -f docker-compose.local.yml exec django uv run python manage.py check_validibot
   ```

7. Create a superuser if needed:
   ```bash
   docker compose -f docker-compose.local.yml exec django uv run python manage.py createsuperuser
   ```

8. Visit http://localhost:8000

## What's running

- `django`: Django app from `compose/local/django/Dockerfile`, mounted with your local code for hot reload (`runserver` on 8000).
- `worker`: Same image, serving internal/task endpoints on port 8001 (lets you mimic separate Cloud Run services with different scaling/concurrency).
- `celery_worker`: Celery worker processing background tasks from Redis queue.
- `celery_beat`: Celery Beat scheduler triggering periodic tasks (purge expired data, cleanup sessions, etc.).
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

4. Run initial setup:

   ```bash
   docker compose -f docker-compose.production.yml exec django python manage.py migrate
   docker compose -f docker-compose.production.yml exec django python manage.py setup_initial_data
   ```

5. Visit http://localhost:8000

Production-style "django" serves user traffic via Gunicorn on port 8000. The "celery_worker" processes background tasks. This is the same stack used for [DigitalOcean deployments](deployment/digitalocean.md).

## VS Code: pytest "Run" button

VS Code's test runner needs environment variables. This repo includes a minimal env file at `.vscode/.env` that points tests at the Docker Postgres (`DATABASE_URL=postgres://validibot:validibot@localhost:5432/validibot`).

If the **Testing** panel hangs, double-check:

- VS Code is using the repo interpreter: `.venv/bin/python` (see `.vscode/settings.json`)
- Docker Compose is running: `docker compose -f docker-compose.local.yml up -d postgres`

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
- `compose/local/django/entrypoint.sh` and `start.sh`: wait for DB, run migrations, start dev server on 8000.
- `compose/local/django/start-worker.sh`: wait for DB, run migrations, start dev server on 8001 for task/internal endpoints.
- `compose/production/django/entrypoint.sh` and `start.sh`: wait for DB, run migrations, collectstatic, start gunicorn on 8000.
- `compose/production/django/start-worker.sh`: wait for DB, run migrations, start gunicorn on 8001 for task/internal endpoints.
