# Google Cloud Deployment

This guide covers deploying Validibot to Google Cloud Run with support for multiple environments (dev, staging, prod).

## Multi-Environment Architecture

Validibot supports three deployment stages:

| Stage | Purpose | Resource Naming |
|-------|---------|-----------------|
| **dev** | Development testing, feature validation | `validibot-web-dev`, `validibot-db-dev` |
| **staging** | Pre-production testing, E2E tests | `validibot-web-staging`, `validibot-db-staging` |
| **prod** | Production environment | `validibot-web`, `validibot-db` |

Each stage has isolated:
- Cloud Run services (web + worker)
- Cloud SQL database instance
- Secrets in Secret Manager
- Cloud Tasks queue
- Service account

Shared across stages:
- GCS buckets (with stage prefixes in paths)
- Artifact Registry (same images, different services)
- Cloud KMS keys

## Quick Start with justfile

All deployment commands accept a stage parameter:

```bash
# Deploy to dev
just gcp-deploy dev
just gcp-deploy-worker dev

# Deploy to production
just gcp-deploy prod
just gcp-deploy-worker prod

# Deploy both services at once
just gcp-deploy-all dev

# Run migrations
just gcp-migrate dev

# View logs
just gcp-logs dev
just gcp-logs prod
```

Run `just` to see all available commands.

## Setting Up a New Environment

To create a new dev or staging environment from scratch:

### Step 1: Initialize Infrastructure

```bash
# Creates service account, database, Cloud Tasks queue, and secret placeholder
just gcp-init-stage dev
```

This command creates:
- Service account: `validibot-cloudrun-dev@PROJECT.iam.gserviceaccount.com`
- Cloud SQL instance: `validibot-db-dev` (db-f1-micro tier)
- Database and user with generated password
- Cloud Tasks queue: `validibot-validation-queue-dev`
- Secret placeholder: `django-env-dev`

**Important**: Save the database password shown in the output!

### Step 2: Configure Environment Secrets

```bash
# Copy template and edit
just gcp-secrets-init dev

# Or manually edit the existing file
vim .envs/.dev/.django
```

Update these values in `.envs/.dev/.django`:
- `DJANGO_SECRET_KEY`: Generate a new key
- `DATABASE_URL`: Use the password from Step 1
- `POSTGRES_PASSWORD`: Same password
- `CLOUD_SQL_CONNECTION_NAME`: Should be `validibot-db-dev`
- `DJANGO_ALLOWED_HOSTS`: Add the dev service URL after first deploy

### Step 3: Upload Secrets

```bash
just gcp-secrets dev
```

### Step 4: Deploy Services

```bash
# Deploy both web and worker
just gcp-deploy-all dev

# Or deploy separately
just gcp-deploy dev
just gcp-deploy-worker dev
```

### Step 5: Run Migrations and Seed Data

```bash
# Run database migrations
just gcp-migrate dev

# Seed initial data (validators, default org, etc.)
just gcp-setup-data dev
```

### Step 6: Verify Deployment

```bash
# Check status
just gcp-status dev

# View logs
just gcp-logs dev

# List all resources
just gcp-list-resources dev
```

## Regular Deployments

For routine code updates after initial setup:

```bash
# Deploy code changes to dev
just gcp-deploy dev

# Deploy to both web and worker
just gcp-deploy-all dev

# Run migrations if needed
just gcp-migrate dev

# Deploy to production
just gcp-deploy-all prod
just gcp-migrate prod
```

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     Google Cloud Platform                    │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐   │
│  │  Cloud Run   │    │  Cloud Run   │    │  Cloud SQL   │   │
│  │  (web)       │───▶│  (worker)    │───▶│  PostgreSQL  │   │
│  │  Port 8000   │    │  Port 8001   │    │              │   │
│  └──────────────┘    └──────────────┘    └──────────────┘   │
│         │                   ▲                               │
│         │                   │ OIDC                          │
│         ▼            ┌──────┴───────┐                       │
│  ┌──────────────┐    │  Cloud       │    ┌──────────────┐   │
│  │  Cloud       │    │  Scheduler   │    │  Cloud       │   │
│  │  Storage     │    │  (cron)      │    │  Secret Mgr  │   │
│  │  (media)     │    └──────────────┘    │  (secrets)   │   │
│  └──────────────┘                        └──────────────┘   │
│                                                              │
│  ┌──────────────┐    ┌──────────────────────────────────┐   │
│  │  Cloud       │    │  Artifact Registry (Docker)       │   │
│  │  Tasks       │    └──────────────────────────────────┘   │
│  │  (async)     │                                           │
│  └──────────────┘                                           │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

## Prerequisites

Before deploying, ensure you have completed the [Setup Cheatsheet](setup-cheatsheet.md):

- [x] gcloud CLI installed and authenticated
- [x] Project configured (`project-a509c806-3e21-4fbc-b19`)
- [x] Required APIs enabled
- [x] Artifact Registry created (`validibot`)
- [x] Docker authentication configured

For production, also ensure:
- [x] Cloud SQL instance created (`validibot-db`)
- [x] Database and user created
- [x] Secret Manager configured (`django-env`)

## Pre-Deployment Checks

Before every deployment, run tests and linting:

```bash
# Run the test suite
uv run --extra dev pytest

# Run linting
uv run --extra dev ruff check
```

Optionally, run Django's deployment security checks against production settings:

```bash
# Check production settings (may require some env vars to be set)
uv run python manage.py check --deploy --settings=config.settings.production
```

## Secrets Management

Each stage has its own secrets file and Secret Manager entry:

| Stage | Local File | Secret Name |
|-------|------------|-------------|
| dev | `.envs/.dev/.django` | `django-env-dev` |
| staging | `.envs/.staging/.django` | `django-env-staging` |
| prod | `.envs/.production/.django` | `django-env` |

To update secrets:

```bash
# Edit the file
vim .envs/.dev/.django

# Upload to Secret Manager
just gcp-secrets dev

# Redeploy to pick up changes
just gcp-deploy dev
```

## Operations

### View Logs

```bash
# Recent logs
just gcp-logs dev

# Follow logs in real-time
just gcp-logs-follow dev

# View job logs (migrations, setup)
just gcp-job-logs validibot-migrate-dev
```

### Check Status

```bash
# Single stage
just gcp-status dev

# All stages
just gcp-status-all
```

### Pause/Resume Service

```bash
# Block public access (useful during maintenance)
just gcp-pause dev

# Restore public access
just gcp-resume dev
```

### List Resources

```bash
# See all resources for a stage
just gcp-list-resources dev
```

## Build and Push Docker Image

The `gcp-deploy` commands handle this automatically, but you can also run manually:

```bash
# Build for Cloud Run (linux/amd64)
just gcp-build

# Push to Artifact Registry
just gcp-push
```

## Troubleshooting

### View detailed logs

```bash
# Real-time logs for web service
gcloud run services logs tail validibot-web-dev --region=australia-southeast1

# Historical logs with filtering
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=validibot-web-dev" --limit=100
```

### Connect to Cloud SQL directly

```bash
# Using Cloud SQL Auth Proxy
gcloud sql connect validibot-db-dev --user=validibot_user --database=validibot
```

### Check secret values

```bash
gcloud secrets versions access latest --secret=django-env-dev
```

### Common issues

**"Secret not found" error:**
```bash
# Ensure secret exists
gcloud secrets describe django-env-dev

# If not, create it
just gcp-secrets dev
```

**"Service account not found" error:**
```bash
# Re-run infrastructure setup
just gcp-init-stage dev
```

**Database connection errors:**
```bash
# Verify Cloud SQL instance is running
gcloud sql instances describe validibot-db-dev --format="value(state)"

# Check connection name in secrets matches instance
```

## Local vs Production

| Aspect        | Local (docker-compose.local.yml) | Production (Cloud Run) |
| ------------- | -------------------------------- | ---------------------- |
| Database      | Local Postgres container         | Cloud SQL              |
| Media storage | Local filesystem                 | Cloud Storage          |
| Secrets       | `.envs/.local/` files            | Secret Manager         |
| Docker images | Built locally                    | Artifact Registry      |
| Scaling       | Single container                 | Auto-scaled (0-N)      |

There is no `docker-compose.production.yml` — production runs on Cloud Run, not Docker Compose.

## Cost Estimates

Monthly costs per stage (approximate, Australia region):

| Stage | Cloud Run | Cloud SQL | Total |
|-------|-----------|-----------|-------|
| dev | ~$5-15 | ~$10 | ~$15-25 |
| staging | ~$5-15 | ~$25 | ~$30-40 |
| prod | ~$10-30 | ~$50 | ~$60-80 |

Dev uses smaller database tiers to minimize costs. All environments scale to zero when not in use.
