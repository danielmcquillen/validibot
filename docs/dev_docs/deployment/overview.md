# Deployment Overview

Validibot supports multiple deployment platforms:

- **Docker Compose**: For VPS, DigitalOcean, on-premises, or any Docker-capable host
- **Google Cloud Platform (GCP)**: Our primary cloud deployment target
- **AWS**: Planned for future release

All deployment commands use the [Just command runner](justfile-guide.md). Commands are organized by platform:

```bash
just gcp deploy prod           # GCP deployment
just docker-compose deploy     # Docker Compose production deployment
just aws deploy prod           # AWS (not yet implemented)
```

This page focuses on GCP deployment. See the [Justfile Guide](justfile-guide.md) for the full command reference.

## Deployment Stages

Validibot uses three isolated deployment stages:

| Stage       | Purpose                                 | Service Name                |
| ----------- | --------------------------------------- | --------------------------- |
| **dev**     | Development testing, feature validation | `$GCP_APP_NAME-web-dev`     |
| **staging** | Pre-production testing, E2E tests       | `$GCP_APP_NAME-web-staging` |
| **prod**    | Production environment                  | `$GCP_APP_NAME-web`         |

Each stage has completely isolated infrastructure:

| Resource             | Dev                                      | Staging                                      | Prod                                 |
| -------------------- | ---------------------------------------- | -------------------------------------------- | ------------------------------------ |
| Cloud SQL instance   | `$GCP_APP_NAME-db-dev`                   | `$GCP_APP_NAME-db-staging`                   | `$GCP_APP_NAME-db`                   |
| Web service          | `$GCP_APP_NAME-web-dev`                  | `$GCP_APP_NAME-web-staging`                  | `$GCP_APP_NAME-web`                  |
| Worker service       | `$GCP_APP_NAME-worker-dev`               | `$GCP_APP_NAME-worker-staging`               | `$GCP_APP_NAME-worker`               |
| EnergyPlus validator | `$GCP_APP_NAME-validator-energyplus-dev` | `$GCP_APP_NAME-validator-energyplus-staging` | `$GCP_APP_NAME-validator-energyplus` |
| FMU validator        | `$GCP_APP_NAME-validator-fmu-dev`        | `$GCP_APP_NAME-validator-fmu-staging`        | `$GCP_APP_NAME-validator-fmu`        |
| Storage bucket       | `$GCP_APP_NAME-storage-dev`              | `$GCP_APP_NAME-storage-staging`              | `$GCP_APP_NAME-storage`              |
| Tasks queue          | `$GCP_APP_NAME-validation-queue-dev`     | `$GCP_APP_NAME-validation-queue-staging`     | `$GCP_APP_NAME-tasks`                |
| Secret               | `django-env-dev`                         | `django-env-staging`                         | `django-env`                         |

!!! note
Resource names are derived from the `GCP_APP_NAME` variable (default: `validibot`), which is set in your `.envs/.production/.google-cloud/.just` config file. If you change `GCP_APP_NAME`, all resource names update automatically.

This isolation ensures that dev/staging changes never affect production data.

## Quick Start

```bash
# Deploy to dev (routine updates)
just gcp deploy dev

# Deploy to production
just gcp deploy prod

# Deploy both web and worker services
just gcp deploy-all dev

# Run migrations
just gcp migrate dev
```

See [Google Cloud Deployment](../google_cloud/deployment.md) for full details.

## Google Cloud Architecture

Each stage runs on Google Cloud Run with the following services:

- **External HTTP(S) Load Balancer** — Custom domain entrypoint (e.g., `validibot.com`) routing to Cloud Run
- **Cloud Run (web)** — Django app serving user traffic
- **Cloud Run (worker)** — Background task processing, validator callbacks
- **Cloud SQL** — PostgreSQL 17 database
- **Cloud Storage** — Media file storage
- **Cloud Tasks** — Optional async task queue for web→worker work and retries (validator Cloud Run Jobs are triggered directly via the Jobs API today)
- **Cloud Scheduler** — Cron jobs (session cleanup, expired key removal)
- **Secret Manager** — Credentials and secrets
- **Artifact Registry** — Docker image storage

See the [Go-Live Checklist](go-live-checklist.md) for pre-launch tasks.

---

## GCP Release Workflow

1. Run the test suite (`uv run --group dev pytest`) and linting locally.
2. Push to GitHub; CI should pass before you merge.
3. Deploy to dev first: `just gcp deploy-all dev`
4. Run migrations: `just gcp migrate dev`
5. Run health check: `python manage.py check_validibot --verbose`
6. Verify on dev, then promote to staging/prod as needed.
7. Check Cloud Logging/Sentry for errors after each deployment.

## Config Vars Reference

Secrets are stored in Secret Manager and mounted as `/secrets/.env`. Key variables:

| Key                        | Notes                                                         |
| -------------------------- | ------------------------------------------------------------- |
| `DJANGO_SECRET_KEY`        | Unique per environment; never reuse local keys.               |
| `DJANGO_ALLOWED_HOSTS`     | Comma-separated hosts, e.g. `validibot.com,*.validibot.com`.  |
| `DATABASE_URL`             | Cloud SQL connection string.                                  |
| `SITE_URL`                 | Public base URL (prod: `https://validibot.com`).              |
| `WORKER_URL`               | Worker base URL (worker `*.run.app` URL) for callbacks/tasks. |
| `STORAGE_BUCKET`           | Cloud Storage bucket (with public/ and private/ prefixes).    |
| `EMAIL_URL` or `ANYMAIL_*` | Postmark/SMTP settings for transactional mail.                |
| `POSTMARK_SERVER_TOKEN`    | Required for waitlist e-mail delivery.                        |
| `SENTRY_DSN`               | Optional but recommended.                                     |

Update secrets with `just gcp secrets <stage>` then redeploy to apply changes.

## Operational Tasks

- **Logs** – `just gcp logs dev` or view in Cloud Console
- **Backups** – Cloud SQL automated backups; test restoration periodically
- **Scaling** – Adjust min/max instances in Cloud Run settings
- **Scheduled tasks** – Managed by Cloud Scheduler (see [scheduled-jobs.md](../google_cloud/scheduled-jobs.md))
