# Running Validibot with Docker

This project uses Docker Compose for local development. All services run in containers,
matching the Cookiecutter Django patterns with Celery + Redis for background task processing.

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
   docker compose up --build
   ```

4. Apply migrations (the start script does this, but you can rerun):
   ```bash
   docker compose exec django uv run python manage.py migrate
   ```

5. Run initial setup (first time only):
   ```bash
   docker compose exec django uv run python manage.py setup_validibot --domain localhost:8000
   ```

6. Verify setup is correct:
   ```bash
   docker compose exec django uv run python manage.py check_validibot
   ```

7. Create a superuser if needed:
   ```bash
   docker compose exec django uv run python manage.py createsuperuser
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

Create production env files from the templates:

For Docker Compose:
```bash
mkdir -p .envs/.production/.Docker Compose
cp .envs.example/.production/.Docker Compose/.django .envs/.production/.Docker Compose/.django
cp .envs.example/.production/.Docker Compose/.postgres .envs/.production/.Docker Compose/.postgres
# Edit with your production values
```

For GCP:
```bash
mkdir -p .envs/.production/.google-cloud
cp .envs.example/.production/.google-cloud/.django .envs/.production/.google-cloud/.django
# Edit with your GCP project values
```

Run:
```bash
docker compose -f docker-compose.production.yml up --build
```

Production-style "web" serves user traffic (gunicorn on 8000). The "worker" runs gunicorn on 8001 for task/internal endpoints so you can mimic separate Cloud Run services with different scaling.

## VS Code: pytest "Run" button

VS Code's test runner needs environment variables. This repo includes a minimal env file at `.vscode/.env` that points tests at the Docker Postgres (`DATABASE_URL=postgres://validibot:validibot@localhost:5432/validibot`).

If the **Testing** panel hangs, double-check:

- VS Code is using the repo interpreter: `.venv/bin/python` (see `.vscode/settings.json`)
- Docker Compose is running: `docker compose up -d postgres`

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
