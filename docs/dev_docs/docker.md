# Running Validibot with Docker (and without)

This project was generated without the Cookiecutter Docker option. The files here mirror Cookiecutter Django's Docker patterns and include Celery + Redis for background task processing.

## Quick start with Docker (local)

1. Ensure your local env files exist:
   - `.envs/.local/.django`
   - `.envs/.local/.postgres` (you can start from `.envs/.local/.postgres.example`)

2. Build and start:  
   `docker compose -f docker-compose.local.yml up --build`

3. Apply migrations (the start script does this, but you can rerun):
   `docker compose -f docker-compose.local.yml exec django uv run python manage.py migrate`

4. Run initial setup (first time only):
   `docker compose -f docker-compose.local.yml exec django uv run python manage.py setup_validibot --domain localhost:8000`

5. Verify setup is correct:
   `docker compose -f docker-compose.local.yml exec django uv run python manage.py check_validibot`

6. Create a superuser if needed:
   `docker compose -f docker-compose.local.yml exec django uv run python manage.py createsuperuser`

7. Visit http://localhost:8000

What's running:

- `django`: Django app from `compose/local/django/Dockerfile`, mounted with your local code for hot reload (`runserver` on 8000).
- `worker`: Same image, serving internal/task endpoints on port 8001 (lets you mimic separate Cloud Run services with different scaling/concurrency).
- `celery_worker`: Celery worker processing background tasks from Redis queue.
- `celery_beat`: Celery Beat scheduler triggering periodic tasks (purge expired data, cleanup sessions, etc.).
- `postgres`: Postgres built from `compose/production/postgres/Dockerfile`, env vars from `.envs/.local/.postgres`.
- `redis`: Redis broker for Celery task queue.
- `mailpit`: Local SMTP capture at http://localhost:8025.

Entrypoint and start scripts live in `compose/local/django/` and wait for Postgres before launching.

## Production-style compose (for parity/testing)

Copy env (GCP example):
`cp .envs/.production/.google-cloud/.django.example .envs/.production/.google-cloud/.django`
`cp .envs/.production/.google-cloud/.postgres.example .envs/.production/.google-cloud/.postgres`

Or for self-hosted:
`cp .envs/.production/.self-hosted/.django.example .envs/.production/.self-hosted/.django`
`cp .envs/.production/.self-hosted/.postgres.example .envs/.production/.self-hosted/.postgres`

Run:
`docker compose -f docker-compose.production.yml up --build`

Production-style "web" serves user traffic (gunicorn on 8000). The "worker" runs gunicorn on 8001 for task/internal endpoints so you can mimic separate Cloud Run services with different scaling.

## Running without Docker

You can still run locally without Docker (often faster for dev):

1. Install Python 3.13 and `uv` (see [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/)).
2. Install deps: `uv sync --extra dev`
3. Decide where Postgres runs:
   - **Local Postgres (default)**: make sure Postgres is running and create the database:
     - `createdb validibot`
   - **Docker Postgres** (optional): start dependencies:
     - `docker compose -f docker-compose.local.yml up -d postgres mailpit`
4. Load env vars for host-run commands:
   - Local Postgres: `source set-env.sh`
   - Docker Postgres: `source set-env.sh docker`
5. Run migrations: `uv run python manage.py migrate`
6. Run initial setup (first time only): `uv run python manage.py setup_validibot --domain localhost:8000`
7. Verify setup: `uv run python manage.py check_validibot`
8. Create a superuser: `uv run python manage.py createsuperuser`
9. Run the server: `uv run python manage.py runserver 0.0.0.0:8000`

If you see `role "validibot" does not exist`, you’re almost certainly connecting to a different Postgres than you think (for example, a local Postgres.app instance on `localhost:5432` instead of the Docker container). Either use local Postgres mode (`source set-env.sh`) or stop the local server so Docker can bind to the port, then use Docker mode (`source set-env.sh docker`).

## VS Code: pytest “Run” button

VS Code’s test runner does not source `set-env.sh`, so it needs a simple env file. This repo includes a minimal one at `.vscode/.env` that points tests at local Postgres (`DATABASE_URL=postgres:///validibot`).

If the **Testing** panel hangs, double-check:

- VS Code is using the repo interpreter: `.venv/bin/python` (see `.vscode/settings.json`)
- Postgres is running and the database exists: `createdb validibot`

## Notes and deviations from full Cookiecutter setup

- **Celery + Redis** handles background tasks and scheduled jobs for self-hosted deployments. For GCP, Cloud Tasks/Scheduler are used instead.
- Static/media: Whitenoise still works in-container, but long-term we’ll move static/media to GCS + CDN as per the GCP ADR.
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
