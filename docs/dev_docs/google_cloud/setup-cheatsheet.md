# Google Cloud Setup Cheatsheet

This document captures the steps taken to set up Validibot on Google Cloud Platform.

## Before You Start

Set these environment variables before running the commands below:

```bash
# Source your config file (copy from .envs.example/.production/.google-cloud/.just first)
source .envs/.production/.google-cloud/.just

# Or set manually:
export GCP_PROJECT_ID="your-project-id"
export GCP_REGION="us-central1"
export GCP_APP_NAME="validibot"  # Resource name prefix used for all GCP resources

# Get your project number (used for some IAM bindings):
export GCP_PROJECT_NUMBER=$(gcloud projects describe $GCP_PROJECT_ID --format="value(projectNumber)")
```

Commands in this guide use `$GCP_PROJECT_ID`, `$GCP_REGION`, `$GCP_PROJECT_NUMBER`, and `$GCP_APP_NAME` as placeholders.

## Prerequisites

### Install gcloud CLI

```bash
# Install via official installer (recommended over Homebrew)
curl https://sdk.cloud.google.com | bash -s -- --disable-prompts --install-dir=$HOME

# Add to your shell profile (~/.zshrc)
source ~/google-cloud-sdk/path.zsh.inc
source ~/google-cloud-sdk/completion.zsh.inc
```

## Initial Setup

### 1. Authenticate with Google Cloud

```bash
# Log in (opens browser for OAuth)
gcloud auth login

# To switch accounts, revoke and re-login
gcloud auth revoke --all
gcloud auth login

# Check current authenticated accounts
gcloud auth list
```

### 2. List and Select Project

```bash
# List available projects
gcloud projects list

# Set the active project
gcloud config set project PROJECT_ID
```

### 3. Rename Project (Display Name Only)

Note: Project IDs cannot be changed after creation, only the display name.

```bash
gcloud projects update PROJECT_ID --name="New Display Name"
```

### 4. Set Default Region

```bash
# Set Australia Southeast as default region
gcloud config set compute/region $GCP_REGION
```

### 5. Verify Configuration

```bash
gcloud config list
```

## Enable Required APIs

Enable all the APIs needed for a Django app on Cloud Run:

```bash
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  cloudtasks.googleapis.com \
  storage.googleapis.com
```

| API                               | Purpose                                  |
| --------------------------------- | ---------------------------------------- |
| `run.googleapis.com`              | Cloud Run (serverless containers)        |
| `sqladmin.googleapis.com`         | Cloud SQL (PostgreSQL database)          |
| `secretmanager.googleapis.com`    | Secret Manager (credentials storage)     |
| `artifactregistry.googleapis.com` | Artifact Registry (Docker images)        |
| `cloudbuild.googleapis.com`       | Cloud Build (CI/CD)                      |
| `cloudtasks.googleapis.com`       | Cloud Tasks (async task queue)           |
| `storage.googleapis.com`          | Cloud Storage (media files)              |

## Create Cloud Tasks Queue

Cloud Tasks is available for async orchestration and retries (for example, moving web→worker work off-request). Validator Cloud Run Jobs are triggered directly via the Jobs API today, but we still provision the queue so we can adopt Cloud Tasks where it adds reliability.

```bash
gcloud tasks queues create $GCP_APP_NAME-tasks \
  --location=$GCP_REGION \
  --project=$GCP_PROJECT_ID
```

Verify the queue was created:

```bash
gcloud tasks queues list --location=$GCP_REGION
```

### Grant permissions to create tasks

The Cloud Run service account needs permission to add tasks to the queue:

```bash
gcloud tasks queues add-iam-policy-binding $GCP_APP_NAME-tasks \
  --location=$GCP_REGION \
  --member="serviceAccount:$GCP_APP_NAME-cloudrun-prod@$GCP_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/cloudtasks.enqueuer"
```

## Next Steps

After completing the above:

1. **Provision Cloud SQL** - Create PostgreSQL instance
2. **Set up Secret Manager** - Store database credentials
3. **Create Artifact Registry** - Docker image repository
4. **Build and Deploy** - Push Docker image and deploy to Cloud Run

## Provision Cloud SQL

Create a PostgreSQL 17 instance (the latest stable version):

```bash
gcloud sql instances create $GCP_APP_NAME-db \
  --database-version=POSTGRES_17 \
  --edition=ENTERPRISE \
  --tier=db-f1-micro \
  --region=$GCP_REGION \
  --storage-type=SSD \
  --storage-size=10GB \
  --availability-type=zonal \
  --backup \
  --backup-start-time=03:00
```

| Option                | Value                  | Notes                                                                      |
| --------------------- | ---------------------- | -------------------------------------------------------------------------- |
| `--database-version`  | `POSTGRES_17`          | Latest stable PostgreSQL (as of Dec 2024)                                  |
| `--edition`           | `ENTERPRISE`           | Required for smaller tiers; `ENTERPRISE_PLUS` requires larger tiers        |
| `--tier`              | `db-f1-micro`          | Smallest/cheapest tier for dev; use `db-g1-small` or larger for production |
| `--region`            | `$GCP_REGION` | Sydney region                                                              |
| `--storage-type`      | `SSD`                  | Better performance                                                         |
| `--storage-size`      | `10GB`                 | Minimum; can auto-grow                                                     |
| `--availability-type` | `zonal`                | Single zone; use `regional` for HA                                         |
| `--backup`            | enabled                | Daily backups                                                              |
| `--backup-start-time` | `03:00`                | UTC time for backup window                                                 |

After creation, create the database and user:

```bash
# Create database
gcloud sql databases create validibot --instance=$GCP_APP_NAME-db

# Generate a strong password
DB_PASSWORD=$(openssl rand -base64 32)
echo "Save this password: $DB_PASSWORD"

# Create user
gcloud sql users create validibot_user \
  --instance=$GCP_APP_NAME-db \
  --password="$DB_PASSWORD"

# Store password in Secret Manager
echo -n "$DB_PASSWORD" | gcloud secrets create db-password --data-file=-
```

## Change Database Password

To change the database password later:

```bash
# Generate new password and store in Secret Manager
NEW_DB_PASSWORD=$(openssl rand -base64 32)
echo -n "$NEW_DB_PASSWORD" | gcloud secrets versions add db-password --data-file=-

# Apply to the database user
gcloud sql users set-password validibot_user \
  --instance=$GCP_APP_NAME-db \
  --password="$(gcloud secrets versions access latest --secret=db-password)"

# Redeploy Cloud Run services to pick up new secret (after deployment)
# gcloud run services update $GCP_APP_NAME-web --region=$GCP_REGION
```

## Create Artifact Registry

Create a Docker repository for storing container images:

```bash
gcloud artifacts repositories create $GCP_APP_NAME \
  --repository-format=docker \
  --location=$GCP_REGION \
  --description="Validibot Docker images"
```

Configure Docker to authenticate with Artifact Registry:

```bash
gcloud auth configure-docker $GCP_REGION-docker.pkg.dev
```

The image URL format is:

```
$GCP_REGION-docker.pkg.dev/$GCP_PROJECT_ID/$GCP_APP_NAME/IMAGE_NAME:TAG
```

## Set Up Secrets

The production environment variables are stored in Secret Manager as a single secret file.

### Why a single .env file instead of per-key secrets?

Cloud Run supports two approaches for secrets:

1. **Per-key secrets** - Each environment variable is a separate secret, injected via `--set-secrets=VAR=secret:version`
2. **File-mounted secret** - A single `.env` file mounted as a volume, sourced by the start script

We use the **file-mounted approach** because:

- **Simpler management** - One secret to create/update instead of 20+
- **Matches local development** - Same `.env` file format used locally
- **Easier migration** - Can copy the local `.envs/.production/.google-cloud/.django` file directly
- **Atomic updates** - All variables update together when you add a new secret version

The tradeoff is less granular access control (all-or-nothing), but for a single-developer project this is acceptable. The start script (`compose/production/django/start.sh`) sources `/secrets/.env` before starting Django.

### Create the django-env secret

> **Important:** GCP environment files are in `.envs/.production/.google-cloud/`.
> Cloud deployments and Docker Compose use `.envs/`, and `source set-env.sh` loads local env vars for host-run commands.

First, update `.envs/.production/.google-cloud/.django` with production values:

- `DJANGO_SECRET_KEY` - Generate with `python3 -c "import secrets; print(secrets.token_urlsafe(50))"`
- `DJANGO_ALLOWED_HOSTS` - `.run.app,.validibot.com`
- `SITE_URL` - Public base URL (typically `https://validibot.com` once the load balancer + DNS is set up)
- `WORKER_URL` - Worker service `*.run.app` URL (used for validator callbacks and scheduled tasks)
- `DATABASE_URL` - Cloud SQL Unix socket format (see below)

The DATABASE_URL format for Cloud SQL:

```
postgres://USER:PASSWORD@/DATABASE?host=/cloudsql/CONNECTION_NAME
```

Note: URL-encode special characters in the password (e.g., `/` becomes `%2F`, `=` becomes `%3D`).

Get the connection name:

```bash
gcloud sql instances describe $GCP_APP_NAME-db --format="value(connectionName)"
# Returns: $GCP_PROJECT_ID:$GCP_REGION:$GCP_APP_NAME-db
```

Then upload the env file as a secret:

```bash
gcloud secrets create django-env \
  --data-file=.envs/.production/.google-cloud/.django \
  --replication-policy=user-managed \
  --locations=$GCP_REGION
```

### Grant Cloud Run access to secrets

```bash
PROJECT_NUMBER=$(gcloud projects describe $GCP_PROJECT_ID --format="value(projectNumber)")

gcloud secrets add-iam-policy-binding django-env \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

### Grant Cloud Run access to Cloud SQL

The Cloud Run service account also needs permission to connect to Cloud SQL:

```bash
gcloud projects add-iam-policy-binding $GCP_PROJECT_ID \
  --member="serviceAccount:$GCP_PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role="roles/cloudsql.client"
```

> **Note for dev environments:** If you create a separate dev Cloud Run service with its own
> service account, you'll need to grant `roles/cloudsql.client` to that service account as well.

### Update a secret

When you change `.envs/.production/.google-cloud/.django`, add a new version:

```bash
gcloud secrets versions add django-env --data-file=.envs/.production/.google-cloud/.django

# Then redeploy Cloud Run to pick up changes
gcloud run services update $GCP_APP_NAME-web --region=$GCP_REGION
```

### List secrets

```bash
gcloud secrets list
gcloud secrets versions list django-env
```

## Create Dedicated Service Account

By default, Cloud Run uses the Compute Engine default service account. For production, create a dedicated
service account with only the permissions needed, following the principle of least privilege.

### Why a dedicated service account?

- **Isolation** - Permissions are specific to Validibot, not shared with other GCP services
- **Auditability** - Logs clearly show which service performed actions
- **Security** - Blast radius is limited if credentials are compromised
- **Environment separation** - Production and staging can have different SAs with different access

### Create the service account

```bash
gcloud iam service-accounts create $GCP_APP_NAME-cloudrun-prod \
  --display-name="Validibot Cloud Run SA (Production)" \
  --description="Service account for Validibot production Cloud Run services" \
  --project $GCP_PROJECT_ID
```

### Grant required roles

The service account needs these roles:

| Role                                 | Purpose                                         |
| ------------------------------------ | ----------------------------------------------- |
| `roles/cloudsql.client`              | Connect to Cloud SQL                            |
| `roles/secretmanager.secretAccessor` | Access secrets mounted via `--set-secrets`      |
| `roles/storage.objectAdmin`          | Read/write media files (when GCS is configured) |

```bash
# Cloud SQL access
gcloud projects add-iam-policy-binding $GCP_PROJECT_ID \
  --member="serviceAccount:$GCP_APP_NAME-cloudrun-prod@$GCP_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/cloudsql.client"

# Secret Manager access (required for custom service accounts with --set-secrets)
gcloud projects add-iam-policy-binding $GCP_PROJECT_ID \
  --member="serviceAccount:$GCP_APP_NAME-cloudrun-prod@$GCP_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

> **Note:** When using a custom service account, Cloud Run requires the SA to have
> `secretmanager.secretAccessor` to access secrets via `--set-secrets`. The default
> compute SA has special implicit access, but custom SAs do not.

### For staging environment (future)

Create a separate service account for staging:

```bash
gcloud iam service-accounts create $GCP_APP_NAME-cloudrun-staging \
  --display-name="Validibot Cloud Run SA (Staging)" \
  --project $GCP_PROJECT_ID

# Grant same roles (but could be more restrictive, e.g., read-only storage)
```

## Create GCS Bucket for Storage

Create a Cloud Storage bucket with prefix-based access control:

```bash
# Production bucket
gcloud storage buckets create gs://$GCP_APP_NAME-storage \
  --location=$GCP_REGION \
  --default-storage-class=STANDARD \
  --uniform-bucket-level-access \
  --project $GCP_PROJECT_ID

# Development bucket
gcloud storage buckets create gs://$GCP_APP_NAME-storage-dev \
  --location=$GCP_REGION \
  --default-storage-class=STANDARD \
  --uniform-bucket-level-access \
  --project $GCP_PROJECT_ID
```

### Grant bucket access to service accounts

```bash
# Production SA -> Production bucket (full access)
gcloud storage buckets add-iam-policy-binding gs://$GCP_APP_NAME-storage \
  --member="serviceAccount:$GCP_APP_NAME-cloudrun-prod@$GCP_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

# Make public/ prefix publicly readable (for avatars, workflow images)
gcloud storage buckets add-iam-policy-binding gs://$GCP_APP_NAME-storage \
  --member="allUsers" \
  --role="roles/storage.objectViewer" \
  --condition='expression=resource.name.startsWith("projects/_/buckets/$GCP_APP_NAME-storage/objects/public/"),title=public-prefix-only'

# Staging SA -> Dev bucket (when staging is set up)
# gcloud storage buckets add-iam-policy-binding gs://$GCP_APP_NAME-storage-dev \
#   --member="serviceAccount:$GCP_APP_NAME-cloudrun-staging@$GCP_PROJECT_ID.iam.gserviceaccount.com" \
#   --role="roles/storage.objectAdmin"
```

Bucket structure:

```
$GCP_APP_NAME-storage/
├── public/      # Publicly readable (avatars, workflow images)
└── private/     # Private (validation submissions, artifacts)
```

Bucket naming:

- `$GCP_APP_NAME-storage` - Production (public/ and private/ prefixes)
- `$GCP_APP_NAME-storage-dev` - Development/staging

The `STORAGE_BUCKET` environment variable in `.envs/.production/.google-cloud/.django` should be set to `$GCP_APP_NAME-storage`.

## Build and Push Docker Image

Build the production Docker image:

```bash
docker build --platform linux/amd64 -f compose/production/django/Dockerfile \
  -t $GCP_REGION-docker.pkg.dev/$GCP_PROJECT_ID/$GCP_APP_NAME/$GCP_APP_NAME-web:v1 .
```

Push to Artifact Registry:

```bash
# Authenticate Docker (one-time setup)
gcloud auth configure-docker $GCP_REGION-docker.pkg.dev

# Push image
docker push $GCP_REGION-docker.pkg.dev/$GCP_PROJECT_ID/$GCP_APP_NAME/$GCP_APP_NAME-web:v1
```

## Deploy to Cloud Run

Deploy the web service with the dedicated service account, secrets, and Cloud SQL connection:

```bash
gcloud run deploy $GCP_APP_NAME-web \
  --image $GCP_REGION-docker.pkg.dev/$GCP_PROJECT_ID/$GCP_APP_NAME/$GCP_APP_NAME-web:v1 \
  --region $GCP_REGION \
  --service-account $GCP_APP_NAME-cloudrun-prod@$GCP_PROJECT_ID.iam.gserviceaccount.com \
  --add-cloudsql-instances $GCP_PROJECT_ID:$GCP_REGION:$GCP_APP_NAME-db \
  --set-secrets=/secrets/.env=django-env:latest \
  --min-instances 0 \
  --max-instances 4 \
  --memory 1Gi \
  --allow-unauthenticated \
  --project $GCP_PROJECT_ID
```

| Option                     | Purpose                                                          |
| -------------------------- | ---------------------------------------------------------------- |
| `--service-account`        | Use dedicated SA instead of default compute SA                   |
| `--add-cloudsql-instances` | Enables Cloud SQL Auth Proxy sidecar                             |
| `--set-secrets`            | Mounts secret as file at `/secrets/.env` (sourced by `start.sh`) |
| `--min-instances 0`        | Scale to zero when idle (cost savings)                           |
| `--max-instances 4`        | Limit max instances for cost control                             |
| `--allow-unauthenticated`  | Public web access (remove for internal services)                 |

After deployment, get the service URL:

```bash
gcloud run services describe $GCP_APP_NAME-web --region=$GCP_REGION --format="value(status.url)"
```

## Running Management Commands

Since Cloud Run doesn't support `exec` into containers, use Cloud Run Jobs for one-off management commands.

**Important:** When using `--command` to override the container entrypoint, the entrypoint script (which loads secrets) is bypassed. You must explicitly source the secrets file in your command.

### Using the justfile (recommended)

The `justfile` provides convenient commands for common operations:

```bash
# Run database migrations
just gcp migrate

# Run setup_validibot (configures site, seeds default data, creates superuser)
just gcp setup-data

# Verify setup is correct
just gcp run-command "check_validibot --verbose"

# View job logs
just gcp job-logs $GCP_APP_NAME-migrate
just gcp job-logs $GCP_APP_NAME-setup
```

### Manual job creation

If you need to run a custom management command:

```bash
gcloud run jobs create $GCP_APP_NAME-manage \
  --image $GCP_REGION-docker.pkg.dev/$GCP_PROJECT_ID/$GCP_APP_NAME/$GCP_APP_NAME-web:latest \
  --region $GCP_REGION \
  --service-account $GCP_APP_NAME-cloudrun-prod@$GCP_PROJECT_ID.iam.gserviceaccount.com \
  --set-cloudsql-instances $GCP_PROJECT_ID:$GCP_REGION:$GCP_APP_NAME-db \
  --set-secrets=/secrets/.env=django-env:latest \
  --memory 1Gi \
  --command "/bin/bash" \
  --args "-c,set -a && source /secrets/.env && set +a && python manage.py YOUR_COMMAND" \
  --project $GCP_PROJECT_ID
```

**Key points:**

- Use `--set-cloudsql-instances` (not `--add-cloudsql-instances`) for jobs
- Use `--command "/bin/bash"` with `--args "-c,..."` to run shell commands
- Must `source /secrets/.env` because `--command` bypasses the entrypoint
- `set -a` exports all variables, `set +a` stops exporting after sourcing

### Execute the job

```bash
gcloud run jobs execute $GCP_APP_NAME-manage --region $GCP_REGION --wait
```

### Check job logs

```bash
gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name=$GCP_APP_NAME-manage" \
  --project $GCP_PROJECT_ID \
  --limit 50 \
  --format="table(timestamp,textPayload)"
```

## Pausing and Resuming the Service

To temporarily block public access without deleting the service:

### Pause (block public traffic)

```bash
gcloud run services update $GCP_APP_NAME-web \
  --region $GCP_REGION \
  --ingress internal \
  --project $GCP_PROJECT_ID
```

This sets ingress to internal-only. The URL will return 403 Forbidden to public requests.
The service can still scale to zero when idle, so you won't incur compute costs.

### Resume (allow public traffic)

```bash
gcloud run services update $GCP_APP_NAME-web \
  --region $GCP_REGION \
  --ingress all \
  --project $GCP_PROJECT_ID
```

> **Note:** You cannot set `--max-instances 0` on Cloud Run - it requires a positive integer.
> Using `--ingress internal` is the recommended way to pause a service.

---

## Your Configuration

Store your project-specific configuration in `.envs/.production/.google-cloud/.just` (not committed to git). See `.envs.example/.production/.google-cloud/.just` for a template.

Key values you'll need:

| Setting                | Example Value                                                      |
| ---------------------- | ------------------------------------------------------------------ |
| Project ID             | `my-project-123456`                                                |
| Region                 | `us-central1`                                                      |
| Cloud SQL Instance     | `$GCP_APP_NAME-db`                                                     |
| Cloud SQL Connection   | `my-project-123456:us-central1:$GCP_APP_NAME-db`                       |
| Artifact Registry      | `us-central1-docker.pkg.dev/my-project-123456/$GCP_APP_NAME/`          |
| Service Account (prod) | `$GCP_APP_NAME-cloudrun-prod@my-project-123456.iam.gserviceaccount.com`|
| Secrets                | `django-env`, `db-password`                                        |
| GCS Bucket (prod)      | `$GCP_APP_NAME-storage` (with public/ and private/ prefixes)        |
| Cloud Tasks Queue      | `$GCP_APP_NAME-tasks`                                                  |
