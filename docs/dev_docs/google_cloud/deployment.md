# Google Cloud Deployment

This guide covers deploying Validibot to Google Cloud Run.

## Quick Start with justfile

The easiest way to deploy is using the `justfile` commands:

```bash
# Code deployment only (use for updates after initial setup)
just gcp-deploy

# Full environment setup (use for NEW environments)
just gcp-setup-all

# Run migrations after deployment
just gcp-migrate

# View logs
just gcp-logs
```

Run `just` to see all available commands. The rest of this document explains what happens under the hood.

### gcp-deploy vs gcp-setup-all

| Command | What it does | When to use |
|---------|--------------|-------------|
| `gcp-deploy` | Builds image, pushes to registry, deploys **web service only** | Regular code deployments (daily/weekly) |
| `gcp-setup-all` | Runs `gcp-deploy` + `gcp-deploy-worker` + `gcp-scheduler-setup` | Initial environment setup (once per environment) |

**Use `gcp-deploy`** for routine code updates. It only touches the web service and is fast.

**Use `gcp-setup-all`** when setting up a new environment (dev, staging, production) for the first time. It deploys both services and configures Cloud Scheduler jobs for background tasks like session cleanup and expired key removal. See [Scheduled Jobs](scheduled-jobs.md) for details on the scheduled tasks.

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
- [x] Cloud SQL instance created (`validibot-db`)
- [x] Database and user created
- [x] Secret Manager configured (`db-password`)
- [x] Artifact Registry created (`validibot`)
- [x] Docker authentication configured

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

This checks for common security misconfigurations (DEBUG=True, missing HTTPS settings, etc.).
Note: This may fail locally if production-only environment variables aren't set.

All tests must pass before deploying to production.

## Build and Push Docker Image

### 1. Test the production Dockerfile locally (optional)

Before pushing to GCP, verify the image builds correctly:

```bash
# Build the production image locally
docker build \
  -f compose/production/django/Dockerfile \
  -t validibot-test \
  .

# Verify it built successfully
docker images | grep validibot-test
```

This catches build errors before pushing to Artifact Registry.

### 2. Build and tag for Artifact Registry

```bash
# Set variables
PROJECT_ID="project-a509c806-3e21-4fbc-b19"
REGION="australia-southeast1"
IMAGE_NAME="validibot-web"
TAG="latest"  # or use git SHA: $(git rev-parse --short HEAD)

# Build with the full Artifact Registry path
docker build \
  --platform linux/amd64
  -f compose/production/django/Dockerfile \
  -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/validibot/${IMAGE_NAME}:${TAG} \
  .
```

### 3. Push to Artifact Registry

```bash
docker push ${REGION}-docker.pkg.dev/${PROJECT_ID}/validibot/${IMAGE_NAME}:${TAG}
```

### 4. Verify the image was pushed

```bash
gcloud artifacts docker images list \
  ${REGION}-docker.pkg.dev/${PROJECT_ID}/validibot
```

## Deploy to Cloud Run

### 1. Create a service account for Cloud Run

```bash
# Create service account
gcloud iam service-accounts create validibot-cloudrun \
  --display-name="Validibot Cloud Run Service Account"

# Grant Cloud SQL Client role
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
  --member="serviceAccount:validibot-cloudrun@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/cloudsql.client"

# Grant Secret Manager access
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
  --member="serviceAccount:validibot-cloudrun@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# Grant Cloud Storage access (for media files)
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
  --member="serviceAccount:validibot-cloudrun@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"
```

### 2. Deploy the web service

```bash
gcloud run deploy validibot-web \
  --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/validibot/${IMAGE_NAME}:${TAG} \
  --region=${REGION} \
  --platform=managed \
  --allow-unauthenticated \
  --service-account=validibot-cloudrun@${PROJECT_ID}.iam.gserviceaccount.com \
  --add-cloudsql-instances=${PROJECT_ID}:${REGION}:validibot-db \
  --set-env-vars="DJANGO_SETTINGS_MODULE=config.settings.production" \
  --set-env-vars="DJANGO_ALLOWED_HOSTS=*.run.app" \
  --set-env-vars="GCS_MEDIA_BUCKET=validibot-au-media" \
  --set-secrets="DATABASE_URL=db-url:latest" \
  --set-secrets="DJANGO_SECRET_KEY=django-secret-key:latest" \
  --port=8000 \
  --memory=512Mi \
  --cpu=1 \
  --min-instances=0 \
  --max-instances=10
```

### 3. Deploy the worker service

```bash
gcloud run deploy validibot-worker \
  --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/validibot/${IMAGE_NAME}:${TAG} \
  --region=${REGION} \
  --platform=managed \
  --no-allow-unauthenticated \
  --service-account=validibot-cloudrun@${PROJECT_ID}.iam.gserviceaccount.com \
  --add-cloudsql-instances=${PROJECT_ID}:${REGION}:validibot-db \
  --set-env-vars="DJANGO_SETTINGS_MODULE=config.settings.production" \
  --set-secrets="DATABASE_URL=db-url:latest" \
  --set-secrets="DJANGO_SECRET_KEY=django-secret-key:latest" \
  --command="/start-worker" \
  --port=8001 \
  --memory=512Mi \
  --cpu=1 \
  --min-instances=0 \
  --max-instances=5
```

## Required Secrets

Validibot uses a single `.env` file stored in Secret Manager. See the [Setup Cheatsheet](setup-cheatsheet.md#set-up-secrets) for details on creating and updating secrets.

To update secrets after editing `.envs/.production/.django`:

```bash
just gcp-secrets
just gcp-deploy  # Redeploy to pick up changes
```

## Run Migrations

After deploying, run migrations using Cloud Run Jobs. The easiest way:

```bash
just gcp-migrate
```

Or manually with the correct command format:

```bash
gcloud run jobs create validibot-migrate \
  --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/validibot/${IMAGE_NAME}:${TAG} \
  --region=${REGION} \
  --service-account=validibot-cloudrun-prod@${PROJECT_ID}.iam.gserviceaccount.com \
  --set-cloudsql-instances=${PROJECT_ID}:${REGION}:validibot-db \
  --set-secrets=/secrets/.env=django-env:latest \
  --memory=1Gi \
  --command="/bin/bash" \
  --args="-c,set -a && source /secrets/.env && set +a && python manage.py migrate --noinput"

# Execute the job
gcloud run jobs execute validibot-migrate --region=${REGION} --wait
```

**Important notes for Cloud Run Jobs:**

- Use `--set-cloudsql-instances` (not `--add-cloudsql-instances`)
- Use `--command "/bin/bash"` with `--args "-c,..."` to run shell commands
- Must source `/secrets/.env` because `--command` bypasses the container entrypoint

## Verify Deployment

```bash
# Get the service URL
gcloud run services describe validibot-web --region=${REGION} --format='value(status.url)'

# Check service status
gcloud run services list --region=${REGION}

# View logs
gcloud run services logs read validibot-web --region=${REGION} --limit=50
```

## Update Deployment

To deploy a new version:

```bash
# Build new image with new tag
NEW_TAG=$(git rev-parse --short HEAD)
docker build \
  --platform linux/amd64
  -f compose/production/django/Dockerfile \
  -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/validibot/${IMAGE_NAME}:${NEW_TAG} \
  .

# Push
docker push ${REGION}-docker.pkg.dev/${PROJECT_ID}/validibot/${IMAGE_NAME}:${NEW_TAG}

# Update the service
gcloud run services update validibot-web \
  --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/validibot/${IMAGE_NAME}:${NEW_TAG} \
  --region=${REGION}

# Run migrations if needed
gcloud run jobs execute validibot-migrate --region=${REGION} --wait
```

## Troubleshooting

### View logs

```bash
# Real-time logs
gcloud run services logs tail validibot-web --region=${REGION}

# Historical logs
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=validibot-web" --limit=100
```

### Connect to Cloud SQL directly

```bash
# Using Cloud SQL Auth Proxy
gcloud sql connect validibot-db --user=validibot_user --database=validibot
```

### Check secret values

```bash
gcloud secrets versions access latest --secret=db-password
gcloud secrets versions access latest --secret=django-secret-key
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
