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
just gcp deploy dev
just gcp deploy-worker dev

# Deploy to production
just gcp deploy prod
just gcp deploy-worker prod

# Deploy both services at once
just gcp deploy-all dev

# Run migrations
just gcp migrate dev

# View logs
just gcp logs dev
just gcp logs prod
```

Run `just` to see all available commands.

## Setting Up a New Environment

The `gcp-init-stage` command works for all stages (dev, staging, prod). The command is idempotent - it checks for existing resources and only creates what's missing, making it safe to re-run.

**Current production resources:**

- Service account: `validibot-cloudrun-prod@PROJECT.iam.gserviceaccount.com`
- Cloud SQL: `validibot-db`
- Cloud Tasks queue: `validibot-tasks`
- GCS bucket: `validibot-storage` (with public/ and private/ prefixes)
- Secret: `django-env`

To create a new environment from scratch (or verify existing resources):

### Step 1: Initialize Infrastructure

```bash
# Creates service account, database, Cloud Tasks queue, GCS buckets, and secret placeholder
just gcp init-stage dev      # For dev environment
just gcp init-stage staging  # For staging environment
just gcp init-stage prod     # For production environment
```

This command creates (example for dev):

- Service account: `validibot-cloudrun-dev@PROJECT.iam.gserviceaccount.com`
- Cloud SQL instance: `validibot-db-dev` (db-f1-micro tier for dev, db-g1-small for staging; prod currently defaults to db-f1-micro—bump before real traffic)
- Database `validibot` and user `validibot_user` with generated password
- Cloud Tasks queue: `validibot-validation-queue-dev`
- GCS bucket: `validibot-storage-dev` (with public/ and private/ prefixes)
- Secret placeholder: `django-env-dev`

For production, resource names have no suffix (e.g., `validibot-db`, `validibot-storage`).

!!! warning "Save the database password!"
    The command outputs a generated password for the database user. **Copy this password immediately** - you'll need it in the next step. If you lose it, you'll need to reset the database user password manually.

??? info "Cloud SQL connectivity and public IP"
    The deploys use the Cloud SQL Auth Proxy via `--add-cloudsql-instances`, which authenticates with IAM instead of IP allowlisting and encrypts traffic. This is a reasonable default for dev/staging and avoids VPC connector costs. If you need network isolation in production, plan a migration to Private IP + Serverless VPC Access and point Cloud Run at the connector; that adds cost/complexity but removes the public IP.

### Step 2: Update Environment File with Password

Edit the appropriate environment file for your stage:

| Stage | Environment File |
|-------|------------------|
| prod | `.envs/.production/.google-cloud/.django` |

Replace `PASSWORD_FROM_GCP_INIT` with the actual password from Step 1. Remember to URL-encode special characters in DATABASE_URL (`/` → `%2F`, `=` → `%3D`):

```
POSTGRES_PASSWORD=<actual-password-here>
DATABASE_URL=postgres://validibot_user:<url-encoded-password>@/validibot?host=/cloudsql/$GCP_PROJECT_ID:$GCP_REGION:<db-instance>
```

Where `<db-instance>` is `validibot-db-dev`, `validibot-db-staging`, or `validibot-db` (for prod).

### Step 3: Upload Secrets to Secret Manager

```bash
just gcp secrets <stage>  # e.g., just gcp secrets dev|staging|prod
```

### Step 4: Deploy Services

```bash
# Deploy both web and worker
just gcp deploy-all <stage>
# e.g., just gcp deploy-all dev|staging|prod
```

### Step 5: Run Migrations and Seed Data

```bash
# Run database migrations
just gcp migrate <stage>

# Seed initial data (validators, default org, etc.)
just gcp setup-data <stage>
```

### Step 6: Deploy Validators

```bash
# Deploy EnergyPlus and FMI validators
just validators-deploy-all <stage>
```

### Step 7: Set Up Scheduled Jobs

```bash
# Create Cloud Scheduler jobs for cleanup tasks
just gcp scheduler-setup <stage>
```

### Step 8: Verify Deployment

```bash
# Check status and get service URL
just gcp status <stage>

# View logs
just gcp logs <stage>

# List all resources
just gcp list-resources <stage>
```

Optionally, update `DJANGO_ALLOWED_HOSTS` in your stage's env file with the service URL, then run `just gcp secrets <stage>` and `just gcp deploy <stage>` again.

## Regular Deployments

For routine code updates after initial setup:

```bash
# Deploy code changes to dev
just gcp deploy dev

# Deploy to both web and worker
just gcp deploy-all dev

# Run migrations if needed
just gcp migrate dev

# Deploy to production
just gcp deploy-all prod
just gcp migrate prod
```

## Custom Domain Setup

There are two ways to map a custom domain to your Cloud Run services. Which one you use depends on your GCP region and requirements.

### Option A: Cloud Run Domain Mappings (simpler)

Cloud Run has a built-in domain mapping feature that handles SSL certificates and DNS routing automatically. This is the simpler option but is only available in certain regions:

**Supported regions:** `asia-east1`, `asia-northeast1`, `asia-southeast1`, `europe-north1`, `europe-west1`, `europe-west4`, `us-central1`, `us-east1`, `us-east4`, `us-west1`

**Not supported:** `australia-southeast1`, `australia-southeast2`, and many others. If your region isn't listed above, you must use Option B.

> **Note:** Domain mappings are still in preview and Google notes they may have latency issues. For high-traffic production deployments, Option B is generally recommended regardless of region.

To set up a domain mapping:

```bash
gcloud beta run domain-mappings create \
  --service validibot-web \
  --domain your-domain.com \
  --region $GCP_REGION \
  --project $GCP_PROJECT_ID
```

Then add the DNS records shown in the command output to your DNS provider.

For full details, see the [Cloud Run domain mapping docs](https://cloud.google.com/run/docs/mapping-custom-domains).

### Option B: Global Application Load Balancer (recommended for production)

A Global external HTTP(S) Load Balancer works with **all** Cloud Run regions and gives you a static IP, CDN integration, and full control over SSL and routing. This is the recommended approach for production, and is **required** for regions that don't support domain mappings (e.g. `australia-southeast1`).

The `justfile` includes an idempotent command that creates the load balancer resources and prints the static IP you need to set in your DNS provider:

```bash
just gcp lb-setup prod validibot.com
```

If you want multiple hostnames on the same cert/load balancer, pass a comma-separated list:

```bash
just gcp lb-setup prod "validibot.com,www.validibot.com"
```

> **Cost:** Global external load balancers have a non-zero base cost even at low traffic. Check [GCP pricing](https://cloud.google.com/vpc/network-pricing#lb) before you commit.

#### DNS records

In your DNS provider, create an `A` record pointing at the static IP printed by `lb-setup`.

- For the apex/root domain (`validibot.com`): create an `A` record for host `@` -> the load balancer IP.
- For `www.validibot.com` (optional): either add another `A` record pointing at the same IP, or use `CNAME www -> validibot.com` (and include `www.validibot.com` in the `lb-setup` domains list so the cert covers it).

#### SSL certificate provisioning

The load balancer uses a Google-managed certificate. After the DNS change propagates, provisioning typically takes 15-60 minutes.

Useful status commands:

```bash
# See the reserved IP (prod)
gcloud compute addresses describe validibot-ip --global \
  --project $GCP_PROJECT_ID

# See certificate status (prod)
gcloud compute ssl-certificates describe validibot-cert --global \
  --project $GCP_PROJECT_ID
```

### App configuration (both options)

- Make sure `DJANGO_ALLOWED_HOSTS` (in `.envs/.production/.django`) includes your domain(s) (for example `validibot.com` and `www.validibot.com`). Then run `just gcp secrets prod` and redeploy.
- Set these base URLs in your env file (they serve different purposes):
  - `SITE_URL`: public web base URL (prod: `https://validibot.com`; dev/staging: the web `*.run.app` URL is fine).
  - `WORKER_URL`: internal worker base URL (the worker `*.run.app` URL). Cloud Run Jobs and Cloud Scheduler target the worker service; callbacks should never go to the public domain.
  You can fetch the current worker URL with:

  ```bash
  gcloud run services describe validibot-worker \
    --region $GCP_REGION \
    --project $GCP_PROJECT_ID \
    --format='value(status.url)'
  ```
- If using Option B, after you confirm the domain works, you can block direct public access to the `*.run.app` URL and only allow traffic via the load balancer:

  ```bash
  gcloud run services update validibot-web \
    --ingress internal-and-cloud-load-balancing \
    --region $GCP_REGION \
    --project $GCP_PROJECT_ID
  ```

### Timeouts (avoiding "30s" surprises)

- Cloud Run request timeouts are configured on the Cloud Run service. This repo deploys with `--timeout 3600s` (see `gcp_cloud_run_request_timeout` in `justfile`).
- Gunicorn is configured to match via `GUNICORN_TIMEOUT_SECONDS` (defaults to `3600`) in `compose/production/django/start.sh` and `compose/production/django/start-worker.sh`.
- If using Option B (load balancer): serverless NEGs do not support customizing the backend-service timeout, and the backend service will show `timeoutSec=30`. If you see requests ending around 30 seconds, check the Cloud Run service `--timeout`, plus any client-side timeouts (browser, reverse proxy, task runner).

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
- [x] Project configured (`$GCP_PROJECT_ID`)
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
| prod | `.envs/.production/.google-cloud/.django` | `django-env` |

To update secrets:

```bash
# Edit the file
vim .envs/.production/.google-cloud/.django

# Upload to Secret Manager
just gcp secrets prod

# Redeploy to pick up changes
just gcp deploy prod
```

## Operations

### View Logs

```bash
# Recent logs
just gcp logs dev

# Follow logs in real-time
just gcp logs-follow dev

# View job logs (migrations, setup)
just gcp job-logs validibot-migrate-dev
```

### Check Status

```bash
# Single stage
just gcp status dev

# All stages
just gcp status-all
```

### Pause/Resume Service

```bash
# Block public access (useful during maintenance)
just gcp pause dev

# Restore public access
just gcp resume dev
```

### List Resources

```bash
# See all resources for a stage
just gcp list-resources dev
```

### Scheduled Jobs (Cloud Scheduler)

```bash
# Set up scheduled jobs for a stage
just gcp scheduler-setup dev
just gcp scheduler-setup prod

# List all scheduler jobs
just gcp scheduler-list

# Run a job manually (for testing)
just gcp scheduler-run validibot-clear-sessions-dev

# Delete all scheduler jobs for a stage
just gcp scheduler-delete-all dev
```

### Validator Jobs

```bash
# Deploy a validator job for a stage
just validator-deploy energyplus dev
just validator-deploy energyplus prod

# List validator jobs
just gcp jobs-list
```

## Build and Push Docker Image

The `gcp-deploy` commands handle this automatically, but you can also run manually:

```bash
# Build for Cloud Run (linux/amd64)
just gcp build

# Push to Artifact Registry
just gcp push
```

## Troubleshooting

### View detailed logs

```bash
# Real-time logs for web service
gcloud run services logs tail validibot-web-dev --region=$GCP_REGION

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
just gcp secrets dev
```

**"Service account not found" error:**
```bash
# Re-run infrastructure setup
just gcp init-stage dev
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
