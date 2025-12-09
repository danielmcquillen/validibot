# Deployment Overview

Validibot runs on Google Cloud Platform with Australian data residency.

## Quick Start

```bash
# Regular code deployment (web service only)
just gcp-deploy

# Full environment setup (first-time deployment)
just gcp-setup-all

# Run migrations after deployment
just gcp-migrate
```

See [Google Cloud Deployment](../google_cloud/deployment.md) for full details.

## Google Cloud Architecture

Production runs on Google Cloud Run with the following services:

- **Cloud Run (web)** — Django app serving user traffic
- **Cloud Run (worker)** — Background task processing, validator callbacks
- **Cloud SQL** — PostgreSQL 17 database
- **Cloud Storage** — Media file storage
- **Cloud Tasks** — Async task queue for validation jobs
- **Cloud Scheduler** — Cron jobs (session cleanup, expired key removal)
- **Secret Manager** — Credentials and secrets
- **Artifact Registry** — Docker image storage

See the [Go-Live Checklist](go-live-checklist.md) for pre-launch tasks.

---

## GCP Release Workflow

1. Run the test suite (`uv run --extra dev pytest`) and linting locally.
2. Push to GitHub; CI should pass before you merge.
3. Deploy with `just gcp-deploy` (web only) or `just gcp-setup-all` (full environment).
4. Run migrations: `just gcp-migrate`
5. For first-time setup, create a superuser and seed data as needed.
6. Verify the site and check Cloud Logging/Sentry for errors.

## Config Vars Reference

Secrets are stored in Secret Manager and mounted as `/secrets/.env`. Key variables:

| Key                        | Notes                                                        |
| -------------------------- | ------------------------------------------------------------ |
| `DJANGO_SECRET_KEY`        | Unique per environment; never reuse local keys.              |
| `DJANGO_ALLOWED_HOSTS`     | Comma-separated hosts, e.g. `validibot.com,*.validibot.com`. |
| `DATABASE_URL`             | Cloud SQL connection string.                                 |
| `GCS_MEDIA_BUCKET`         | Cloud Storage bucket for media files.                        |
| `EMAIL_URL` or `ANYMAIL_*` | Postmark/SMTP settings for transactional mail.               |
| `POSTMARK_SERVER_TOKEN`    | Required for waitlist e-mail delivery.                       |
| `SENTRY_DSN`               | Optional but recommended.                                    |

Update secrets with `just gcp-secrets` then redeploy to apply changes.

## Operational Tasks

- **Logs** – `just gcp-logs` or view in Cloud Console
- **Backups** – Cloud SQL automated backups; test restoration periodically
- **Scaling** – Adjust min/max instances in Cloud Run settings
- **Scheduled tasks** – Managed by Cloud Scheduler (see [scheduled-jobs.md](../google_cloud/scheduled-jobs.md))

---

## Archived: Legacy Heroku

!!! warning "Deprecated"
    Heroku deployment is no longer supported. This section is retained for historical reference only. The platform migrated to Google Cloud in December 2024.

See [heroku.md](heroku.md) for the archived Heroku deployment guide.
