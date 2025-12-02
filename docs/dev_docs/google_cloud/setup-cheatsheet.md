# Google Cloud Setup Cheatsheet

This document captures the steps taken to set up Validibot on Google Cloud Platform.

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
gcloud config set compute/region australia-southeast1
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

| API                               | Purpose                              |
| --------------------------------- | ------------------------------------ |
| `run.googleapis.com`              | Cloud Run (serverless containers)    |
| `sqladmin.googleapis.com`         | Cloud SQL (PostgreSQL database)      |
| `secretmanager.googleapis.com`    | Secret Manager (credentials storage) |
| `artifactregistry.googleapis.com` | Artifact Registry (Docker images)    |
| `cloudbuild.googleapis.com`       | Cloud Build (CI/CD)                  |
| `cloudtasks.googleapis.com`       | Cloud Tasks (async task queue)       |
| `storage.googleapis.com`          | Cloud Storage (media files)          |

## Next Steps

After completing the above:

1. **Provision Cloud SQL** - Create PostgreSQL instance
2. **Set up Secret Manager** - Store database credentials
3. **Create Artifact Registry** - Docker image repository
4. **Build and Deploy** - Push Docker image and deploy to Cloud Run

## Provision Cloud SQL

Create a PostgreSQL 17 instance (the latest stable version):

```bash
gcloud sql instances create validibot-db \
  --database-version=POSTGRES_17 \
  --edition=ENTERPRISE \
  --tier=db-f1-micro \
  --region=australia-southeast1 \
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
| `--region`            | `australia-southeast1` | Sydney region                                                              |
| `--storage-type`      | `SSD`                  | Better performance                                                         |
| `--storage-size`      | `10GB`                 | Minimum; can auto-grow                                                     |
| `--availability-type` | `zonal`                | Single zone; use `regional` for HA                                         |
| `--backup`            | enabled                | Daily backups                                                              |
| `--backup-start-time` | `03:00`                | UTC time for backup window                                                 |

After creation, create the database and user:

```bash
# Create database
gcloud sql databases create validibot --instance=validibot-db

# Generate a strong password
DB_PASSWORD=$(openssl rand -base64 32)
echo "Save this password: $DB_PASSWORD"

# Create user
gcloud sql users create validibot_user \
  --instance=validibot-db \
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
  --instance=validibot-db \
  --password="$(gcloud secrets versions access latest --secret=db-password)"

# Redeploy Cloud Run services to pick up new secret (after deployment)
# gcloud run services update validibot-web --region=australia-southeast1
```

## Create Artifact Registry

Create a Docker repository for storing container images:

```bash
gcloud artifacts repositories create validibot \
  --repository-format=docker \
  --location=australia-southeast1 \
  --description="Validibot Docker images"
```

Configure Docker to authenticate with Artifact Registry:

```bash
gcloud auth configure-docker australia-southeast1-docker.pkg.dev
```

The image URL format is:

```
australia-southeast1-docker.pkg.dev/PROJECT_ID/validibot/IMAGE_NAME:TAG
```

---

## Validibot-Specific Configuration

| Setting      | Value                            |
| ------------ | -------------------------------- |
| Project Name | Validibot                        |
| Project ID   | `project-a509c806-3e21-4fbc-b19` |
| Region       | `australia-southeast1`           |
| Account      | daniel@mcquilleninteractive.com  |
