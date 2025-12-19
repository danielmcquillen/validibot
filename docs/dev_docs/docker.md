# Running Validibot with Docker (and without)

This project was generated without the Cookiecutter Docker option. The files here mirror Cookiecutter Django’s Docker patterns but are simplified for our stack (no Celery/Redis; Cloud Tasks is our queue).

## Quick start with Docker (local)

1. Ensure your local env files exist:
   - `.envs/.local/.django`
   - `.envs/.local/.postgres` (you can start from `.envs/.local/.postgres.example`)

2. Build and start:  
   `docker compose -f docker-compose.local.yml up --build`

3. Apply migrations (the start script does this, but you can rerun):  
   `docker compose -f docker-compose.local.yml exec django uv run python manage.py migrate`

4. Create a superuser if needed:  
   `docker compose -f docker-compose.local.yml exec django uv run python manage.py createsuperuser`

5. Visit http://localhost:8000

What’s running:

- `django`: Django app from `compose/local/django/Dockerfile`, mounted with your local code for hot reload (`runserver` on 8000).
- `worker`: Same image, serving internal/task endpoints on port 8001 (lets you mimic separate Cloud Run services with different scaling/concurrency).
- `postgres`: Postgres built from `compose/production/postgres/Dockerfile`, env vars from `.envs/.local/.postgres`.
- `mailpit`: Local SMTP capture at http://localhost:8025.

Entrypoint and start scripts live in `compose/local/django/` and wait for Postgres before launching.

## Production-style compose (for parity/testing)

Copy env:  
`cp .envs/.production/.django.example .envs/.production/.django`  
`cp .envs/.production/.postgres.example .envs/.production/.postgres`

Run:  
`docker compose -f docker-compose.production.yml up --build`

Production-style “web” serves user traffic (gunicorn on 8000). The “worker” runs gunicorn on 8001 for task/internal endpoints so you can mimic separate Cloud Run services with different scaling. Postgres is built from the same Dockerfile as local but uses `.envs/.production/.postgres`.

## Running without Docker

You can still run locally without Docker (often faster for dev):

1. Install Python 3.13 and `uv` (see [uv installation guide](https://docs.astral.sh/uv/getting-started/installation/)).
2. Install deps: `uv sync --extra dev`
3. Start dependencies (optional, if you want to reuse the Docker Postgres):
   - `docker compose -f docker-compose.local.yml up -d postgres mailpit`
4. Load env vars for host-run commands:
   - `source set-env.sh`
   - Note: `set-env.sh` rewrites `POSTGRES_HOST=postgres` to `localhost` so `DATABASE_URL` works outside Docker.
5. Run migrations: `uv run python manage.py migrate`
6. Run the server: `uv run python manage.py runserver 0.0.0.0:8000`

## Notes and deviations from full Cookiecutter setup

- No Celery/Redis services: we use Google Cloud Tasks for async work, so the compose files only include `web` and `db`.
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
