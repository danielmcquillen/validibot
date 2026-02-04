# Environment Configuration Templates

This directory contains example environment files for different deployment scenarios.
Copy these to `.envs/` and edit with your actual values.

> ⚠️ **Security Warning**: The `.envs/` folder is gitignored and must NEVER be committed to version control, especially public repositories. It contains passwords, API keys, and other sensitive credentials. Only `.envs.example/` (this folder) should be committed.

## Quick Start

### Local Development

All services run in Docker containers. This is the simplest setup - no local
Postgres or Redis installation required.

```bash
# Create the .envs directory structure
mkdir -p .envs/.local

# Copy the templates
cp .envs.example/.local/.django .envs/.local/.django
cp .envs.example/.local/.postgres .envs/.local/.postgres

# Edit the files and replace !!!SET...!!! placeholders with your values
# Then start Docker Compose:
docker compose up
```

**What runs where:**
- Django: Docker container (port 8000)
- Postgres: Docker container (port 5432)
- Redis: Docker container (port 6379)
- Celery worker: Docker container

### Docker Compose Production

```bash
# Create the directory structure
mkdir -p .envs/.production/.docker-compose

# Copy both files
cp .envs.example/.production/.docker-compose/.django .envs/.production/.docker-compose/.django
cp .envs.example/.production/.docker-compose/.postgres .envs/.production/.docker-compose/.postgres

# Edit with your production values (especially secrets!)
# Then start with:
docker compose -f docker-compose.production.yml up -d
```

### Google Cloud Platform (Cloud Run)

```bash
# Create the directory structure
mkdir -p .envs/.production/.google-cloud

# Copy the template
cp .envs.example/.production/.google-cloud/.django .envs/.production/.google-cloud/.django

# Edit with your GCP project values
# Deploy via Cloud Build or your CI/CD pipeline
```

### AWS (Future)

```bash
# Create the directory structure
mkdir -p .envs/.production/.aws

# Copy the template
cp .envs.example/.production/.aws/.django .envs/.production/.aws/.django

# Edit with your AWS values
# Note: AWS deployment is planned but not yet implemented
```

## Directory Structure

```
.envs.example/              # Templates (committed to git)
├── README.md
├── .local/
│   ├── .django             # Django settings for local dev
│   └── .postgres           # Postgres credentials for local dev
└── .production/
    ├── .docker-compose/
    │   ├── .django
    │   └── .postgres
    ├── .google-cloud/
    │   └── .django
    └── .aws/
        └── .django

.envs/                      # Your actual secrets (NOT committed - gitignored)
├── .local/
│   ├── .django
│   └── .postgres
└── .production/
    ├── .docker-compose/
    │   ├── .django
    │   └── .postgres
    ├── .google-cloud/
    │   └── .django
    └── .aws/
        └── .django
```

## Environment Variable Reference

### PostgreSQL Variables (`.postgres`)

| Variable | Description | Default |
|----------|-------------|---------|
| `POSTGRES_HOST` | Database hostname | `postgres` (Docker service name) |
| `POSTGRES_PORT` | Database port | `5432` |
| `POSTGRES_DB` | Database name | `validibot` |
| `POSTGRES_USER` | Database user | - |
| `POSTGRES_PASSWORD` | Database password | - |

**Note:** `DATABASE_URL` is automatically constructed by the entrypoint script from these variables.

### Django Variables (`.django`)

#### Core Settings

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `DJANGO_SETTINGS_MODULE` | Settings module path | `config.settings.local` | Yes |
| `DJANGO_SECRET_KEY` | Secret key for cryptographic signing | - | Production only |
| `DJANGO_DEBUG` | Enable debug mode | `True` (local) | No |
| `DJANGO_ALLOWED_HOSTS` | Comma-separated list of allowed hosts | `*` (local) | Production only |
| `DJANGO_ADMIN_URL` | Admin URL path | `admin/` | No |
| `DEPLOYMENT_TARGET` | Deployment platform (`docker_compose`, `gcp`, `aws`) | - | Production only |

#### Infrastructure

| Variable | Description | Default |
|----------|-------------|---------|
| `USE_DOCKER` | Running in Docker container | `yes` |
| `REDIS_URL` | Redis connection URL | `redis://redis:6379/0` |

#### Email (Optional)

| Variable | Description |
|----------|-------------|
| `POSTMARK_SERVER_TOKEN` | Postmark API token |
| `MAILGUN_API_KEY` | Mailgun API key |
| `SENDGRID_API_KEY` | SendGrid API key |

If no email provider is configured, emails are printed to the console.

#### Feature Toggles

| Variable | Description | Default |
|----------|-------------|---------|
| `DJANGO_ACCOUNT_ALLOW_REGISTRATION` | Allow new user signups | `true` |
| `DJANGO_ACCOUNT_ALLOW_LOGIN` | Allow user login | `true` |
| `ENABLE_AI_VALIDATIONS` | Enable AI-powered validators | `false` |

#### Superuser (Initial Setup)

| Variable | Description | Default |
|----------|-------------|---------|
| `SUPERUSER_USERNAME` | Admin username | `admin` |
| `SUPERUSER_PASSWORD` | Admin password | - |
| `SUPERUSER_EMAIL` | Admin email | `admin@example.com` |
| `SUPERUSER_NAME` | Admin display name | `Admin` |

#### Celery (Optional)

| Variable | Description | Default |
|----------|-------------|---------|
| `CELERY_FLOWER_USER` | Flower UI username | `debug` |
| `CELERY_FLOWER_PASSWORD` | Flower UI password | `debug` |

#### Production Security

| Variable | Description | Default |
|----------|-------------|---------|
| `DJANGO_SECURE_SSL_REDIRECT` | Redirect HTTP to HTTPS | `true` |
| `SENTRY_DSN` | Sentry error tracking DSN | - |
| `WEB_CONCURRENCY` | Gunicorn worker count | `4` |

#### GCP-Specific (Google Cloud)

| Variable | Description |
|----------|-------------|
| `GCP_PROJECT_ID` | Google Cloud project ID |
| `GCP_REGION` | Google Cloud region |
| `CLOUD_SQL_CONNECTION_NAME` | Cloud SQL instance connection name |
| `STORAGE_BUCKET` | GCS bucket for file storage |
| `GCS_TASK_QUEUE_NAME` | Cloud Tasks queue name |
| `CLOUD_TASKS_SERVICE_ACCOUNT` | Service account for Cloud Tasks |

## Important Notes

1. **NEVER commit `.envs/` to version control** - This folder contains your real secrets and is gitignored. Committing it to a public repository would expose passwords, API keys, and other sensitive credentials.
2. **Generate real secrets** - Use the commands below to generate `DJANGO_SECRET_KEY` and passwords for production
3. **Platform-specific settings** - Each template includes only settings relevant to that deployment target
4. **Placeholder values** - Replace all `!!!SET...!!!` placeholders with actual values before running
5. **DATABASE_URL** - Automatically constructed by entrypoint; don't set manually for Docker Compose deployments
6. **Use different secrets per environment** - Dev, staging, and production should have completely different credentials

## Generating Secrets

### Django Secret Key

```bash
python -c 'from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())'
```

### Secure Password

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```
