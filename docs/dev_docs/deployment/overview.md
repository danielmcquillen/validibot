# Deployment Overview

Validibot is migrating from Heroku to Google Cloud Platform. This section covers
both deployment targets during the transition.

## Deployment Targets

| Target           | Status         | Documentation                                            |
| ---------------- | -------------- | -------------------------------------------------------- |
| **Google Cloud** | 🚧 In Progress | [Google Cloud Deployment](../google_cloud/deployment.md) |
| **Heroku**       | 📦 Legacy      | [Heroku Deployment](heroku.md)                           |

## Google Cloud Architecture

Production runs on Google Cloud Run with the following services:

- **Cloud Run (web)** — Django app serving user traffic
- **Cloud Run (worker)** — Background task processing
- **Cloud SQL** — PostgreSQL 17 database
- **Cloud Storage** — Media file storage
- **Cloud Tasks** — Async task queue
- **Secret Manager** — Credentials and secrets
- **Artifact Registry** — Docker image storage

See the [Go-Live Checklist](go-live-checklist.md) for pre-launch tasks.

---

## Legacy: Heroku

### Environments

- **Production** – single Heroku app running the ASGI web dyno, Celery worker, and
  Celery beat. Uses Heroku Postgres, Redis (Heroku Data for Redis), and S3 for media.
- **Review apps / staging** – create as-needed from the production template; copy the
  config vars called out below and point to disposable add-ons.

### Release Workflow

1. Run the test suite (`uv run --extra dev pytest`) and linting hooks locally.
2. Push to GitHub; CI should pass before you promote a commit.
3. Deploy to Heroku using either the GitHub integration or `git push heroku main`.
4. Heroku executes the `release` stage from `Procfile`, which runs
   `python manage.py migrate`.
5. After the very first deploy (or after a migration reset) run
   `heroku run python manage.py setup_all` to seed roles, personal workspaces, and the
   default AI validator.
6. Verify the site (`/`), the Celery worker dashboard logs, and Sentry for errors. Fatal
   errors will not e-mail `ADMINS`; Sentry is the source of alerting for production
   crashes.

## Config Vars Cheat Sheet

| Key                        | Notes                                                        |
| -------------------------- | ------------------------------------------------------------ |
| `DJANGO_SECRET_KEY`        | Unique per environment; never reuse local keys.              |
| `DJANGO_ALLOWED_HOSTS`     | Comma-separated hosts, e.g. `validibot.com,*.validibot.com`. |
| `DATABASE_URL`             | Managed by Postgres add-on.                                  |
| `REDIS_URL`                | Provided by Redis add-on; shared by Django cache and Celery. |
| `DJANGO_AWS_*`             | S3 credentials and bucket info for media.                    |
| `EMAIL_URL` or `ANYMAIL_*` | Postmark/SMTP settings for transactional mail.               |
| `POSTMARK_SERVER_TOKEN`    | Required for waitlist e-mail delivery.                       |
| `SENTRY_DSN`               | Optional but recommended.                                    |

Keep `_envs/production/django.env` updated with the canonical set; use it when
you need to bootstrap a new Heroku app.

## Operational Tasks

- **Static assets** – `bin/post_compile` runs `collectstatic` and `compilemessages` in
  the slug build phase. No manual action required after deploys.
- **Backups** – enable Heroku Postgres automatic backups and verify you can restore
  them. Use `heroku pg:backups` to inspect and `heroku pg:backups:restore` when needed.
- **Logs** – `heroku logs --tail -p web` for the ASGI dyno, `-p worker`/`-p beat` for
  Celery. Attach Papertrail or another log drain for retention.
- **Scaling** – adjust dyno counts with `heroku ps:scale web=2 worker=2 beat=1`.
- **Maintenance mode** – `heroku maintenance:on` before running destructive tasks
  (e.g., restoring a backup).

The [Heroku deployment guide](heroku.md) contains the copy/paste commands and
per-app defaults. See also the [important notes](important_notes.md) for critical
config reminders.

## ASGI vs WSGI

Validibot can run either as an ASGI app (for long-lived connections and
websockets) or as a traditional WSGI app. The key touch points are:

- `config/asgi.py` and `config/wsgi.py` expose the respective entrypoints. The ASGI
  wrapper routes HTTP to Django and websockets to `config.websocket`.
- `config/settings/base.py` currently sets `WSGI_APPLICATION = "config.wsgi.application"`.
  To run fully async, flip that to `ASGI_APPLICATION = "config.asgi.application"`
  (and comment out the WSGI setting) so Django knows which callable to import.
- `Procfile` controls what Heroku launches. The existing `web` dyno uses
  `gunicorn config.wsgi:application -k uvicorn_worker.UvicornWorker`, which keeps the
  ASGI worker ready while still bootstrapping the WSGI module. To run in pure WSGI
  mode remove the `-k uvicorn_worker.UvicornWorker` flag. To go full ASGI switch the
  command to `gunicorn config.asgi:application -k uvicorn_worker.UvicornWorker`.

Whenever you toggle modes, update both the settings file and the Procfile entry in
the same commit, then re-deploy so Heroku rebuilds the slug with the new interface.
